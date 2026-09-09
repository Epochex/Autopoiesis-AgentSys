"""Durable, approved changes. A crash or ambiguous device reply never means success.

Adapters produce bounded plans; callers cannot provide commands or REST paths.
Only a persisted, digest-bound approval permits an execution. Device leases are
durable and deliberately remain held after process death until reconciliation.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol


class ChangeError(ValueError):
    pass


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


class Adapter(Protocol):
    def identify(self) -> dict: ...
    def prepare(self, action: str, target: dict, params: dict) -> dict: ...
    def read(self, operation: dict) -> Any: ...
    def apply(self, operation: dict, value: Any) -> dict: ...
    def observe(self, plan: dict) -> dict: ...


class ChangeStore:
    def __init__(self, path: str | Path, *, on_event=None):
        self.on_event = on_event
        self._pending_events = {}
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS changes(id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY, change_id TEXT NOT NULL, at REAL NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS leases(device TEXT PRIMARY KEY, change_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, actor TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS rule_uses(rule_id TEXT NOT NULL, change_id TEXT UNIQUE NOT NULL, at REAL NOT NULL);
            ''')
        os.chmod(self.path, 0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        self._pending_events[id(db)] = []
        try:
            with db:
                yield db
            # Stream receipts only after their transaction is durable.
            for event in self._pending_events.pop(id(db), []):
                if self.on_event is not None:
                    try:
                        self.on_event(event)
                    except Exception:
                        pass  # The durable event remains available for projection retry.
        finally:
            self._pending_events.pop(id(db), None)
            db.close()

    def create(self, payload: dict):
        with self.db() as db:
            db.execute('INSERT INTO changes VALUES(?,?,?)', (payload['id'], payload['status'], json.dumps(payload)))
            self.event(payload['id'], 'prepared', {'digest': payload['digest'], 'actor': payload['actor']}, db=db)

    def event(self, cid: str, kind: str, payload: dict, *, db=None):
        if db is None:
            with self.db() as connection:
                self.event(cid, kind, payload, db=connection)
        else:
            at = time.time()
            cursor = db.execute('INSERT INTO events(change_id,at,kind,payload) VALUES(?,?,?,?)', (cid, at, kind, json.dumps(payload)))
            self._pending_events.setdefault(id(db), []).append({'change_id': cid, 'seq': cursor.lastrowid,
                'at': at, 'kind': kind, 'payload': payload})

    def get(self, cid: str) -> dict:
        with self.db() as db:
            row = db.execute('SELECT payload FROM changes WHERE id=?', (cid,)).fetchone()
            if not row:
                raise ChangeError('change_not_found')
            result = json.loads(row['payload'])
            result['events'] = [dict(r) | {'payload': json.loads(r['payload'])} for r in db.execute('SELECT at,kind,payload FROM events WHERE change_id=? ORDER BY seq', (cid,))]
            return result

    def list(self, limit=50) -> list[dict]:
        with self.db() as db:
            return [json.loads(r['payload']) for r in db.execute('SELECT payload FROM changes ORDER BY rowid DESC LIMIT ?', (limit,))]

    def transition(self, cid: str, expected: tuple[str, ...], status: str, extra: dict | None = None, *, lease=False, release=False):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT payload FROM changes WHERE id=?', (cid,)).fetchone()
            if not row:
                raise ChangeError('change_not_found')
            p = json.loads(row['payload'])
            if p['status'] not in expected:
                raise ChangeError('state_changed_refresh_required')
            if lease:
                try:
                    db.execute('INSERT INTO leases VALUES(?,?)', (p['device_id'], cid))
                except sqlite3.IntegrityError as exc:
                    raise ChangeError('device_has_active_or_unreconciled_change') from exc
            if release:
                db.execute('DELETE FROM leases WHERE change_id=?', (cid,))
            p.update(extra or {})
            p['status'] = status
            p['updated_at'] = time.time()
            db.execute('UPDATE changes SET status=?,payload=? WHERE id=?', (status, json.dumps(p), cid))
            self.event(cid, status, extra or {}, db=db)
            return p


class ChangeService:
    def __init__(self, store: ChangeStore, adapter_for, paused=lambda: False, sleep=time.sleep):
        self.store, self.adapter_for, self.paused, self.sleep = store, adapter_for, paused, sleep

    def prepare(self, device_id: str, action: str, target: dict, params: dict, actor: str, reason: str, case_id: str = ''):
        adapter = self.adapter_for(device_id)
        try:
            return self._prepare(adapter, device_id, action, target, params, actor, reason, case_id)
        finally:
            if hasattr(adapter, 'close'):
                adapter.close()

    def _prepare(self, adapter, device_id, action, target, params, actor, reason, case_id):
        if not reason.strip():
            raise ChangeError('reason_required')
        identity = adapter.identify()
        plan = adapter.prepare(action, target, params)
        if not plan.get('operations'):
            raise ChangeError('no_configuration_difference')
        baseline = adapter.observe(plan)
        if not baseline.get('management_ok') or not baseline.get('guard_ok'):
            raise ChangeError('management_or_business_guard_unhealthy')
        cid = uuid.uuid4().hex
        p = dict(id=cid, device_id=device_id, action=action, target=target, params=params,
                 identity=identity, plan=plan, baseline=baseline, actor=actor, reason=reason,
                 case_id=case_id, status='awaiting_approval', created_at=time.time(),
                 expires_at=time.time() + 900)
        p['digest'] = digest(p)
        self.store.create(p)
        return self.store.get(cid)

    def approve(self, cid: str, expected_digest: str, actor: str, reason: str):
        p = self.store.get(cid)
        if p['digest'] != expected_digest or p['expires_at'] <= time.time() or not reason.strip():
            raise ChangeError('approval_expired_or_digest_mismatch')
        return self.store.transition(cid, ('awaiting_approval',), 'approved',
            {'approval': {'actor': actor, 'reason': reason, 'digest': expected_digest, 'at': time.time()}})

    def preauthorize(self, cid: str, rules: list[dict]):
        """Root-managed exact rules. No wildcard devices/actions or user-supplied rules."""
        p = self.store.get(cid)
        if p['status'] != 'awaiting_approval' or p['expires_at'] <= time.time():
            raise ChangeError('change_not_eligible_for_preauthorization')
        for rule in rules:
            if (rule.get('device_id') != p['device_id'] or rule.get('action') != p['action']
                or rule.get('target') != p['target'] or rule.get('params') != p['params']
                or rule.get('expires_at', 0) <= time.time()
                or rule.get('plan_digest') != digest(p['plan'])):
                continue
            with self.store.db() as db:
                db.execute('BEGIN IMMEDIATE')
                uses = db.execute('SELECT count(*) FROM rule_uses WHERE rule_id=?', (rule['id'],)).fetchone()[0]
                if uses >= min(10, int(rule.get('max_uses', 1))):
                    continue
                db.execute('INSERT INTO rule_uses VALUES(?,?,?)', (rule['id'], cid, time.time()))
            return self.approve(cid, p['digest'], 'preauthorization:' + rule['id'], 'exact registered rule')
        raise ChangeError('no_matching_preauthorization')

    def execute(self, cid: str, actor: str):
        p = self.store.get(cid)
        if p['status'] in ('succeeded', 'rolled_back', 'needs_human', 'expired', 'cancelled', 'restored'):
            return p  # Same change ID cannot run twice.
        if self.paused():
            raise ChangeError('all_device_writes_paused')
        if p['expires_at'] <= time.time():
            self.store.transition(cid, ('approved', 'awaiting_approval'), 'expired')
            raise ChangeError('plan_expired_prepare_again')
        if p.get('approval', {}).get('digest') != p['digest']:
            raise ChangeError('approval_required')
        p = self.store.transition(cid, ('approved',), 'executing', {'executed_by': actor}, lease=True)
        attempted = []
        adapter = None
        try:
            adapter = self.adapter_for(p['device_id'])
            if adapter.identify() != p['identity']:
                raise ChangeError('device_identity_changed')
            for precondition in p['plan'].get('preconditions', []):
                if adapter.read(precondition) != precondition['before']:
                    raise ChangeError('related_configuration_changed_since_preview')
            # Check every baseline before the first mutation, including multi-step operations.
            seen = set()
            for op in p['plan']['operations']:
                key = op['key']
                if key not in seen and not op.get('deferred') and adapter.read(op) != op['before']:
                    raise ChangeError('configuration_changed_since_preview')
                seen.add(key)
            baseline = adapter.observe(p['plan'])
            if not baseline.get('management_ok') or not baseline.get('guard_ok'):
                raise ChangeError('guard_failed_before_execution')
            for index, op in enumerate(p['plan']['operations']):
                if self.paused() or self.store.get(cid).get('abort_requested'):
                    raise ChangeError('writes_paused_during_change')
                if adapter.read(op) != op['before']:
                    raise ChangeError('configuration_changed_before_operation')
                self.store.event(cid, 'operation_started', {'index': index, 'key': op['key']})
                attempted.append(op)  # Include timed-out commands in recovery reconciliation.
                receipt = adapter.apply(op, op['after'])
                self.store.event(cid, 'operation_receipt', {'index': index, 'receipt': receipt})
                if op.get('settle_seconds'):
                    self.sleep(op['settle_seconds'])
                if not op.get('irreversible') and adapter.read(op) != op['after']:
                    raise ChangeError('configuration_readback_failed')
            self.store.transition(cid, ('executing',), 'observing')
            readings = []
            bad_target = 0
            for i in range(p['plan'].get('observation_samples', 12)):
                if i:
                    self.sleep(p['plan'].get('observation_interval', 5))
                if self.store.get(cid).get('abort_requested'):
                    raise ChangeError('operator_requested_restore')
                observation = adapter.observe(p['plan'])
                self.store.event(cid, 'business_readback', observation)
                readings.append(observation)
                if not all(observation.get(k) is True for k in ('management_ok', 'guard_ok')):
                    raise ChangeError('management_or_business_guard_failed')
                bad_target = 0 if observation.get('target_ok') is True else bad_target + 1
                if bad_target >= 2 and i >= p['plan'].get('grace_samples', 2):
                    raise ChangeError('business_readback_failed_or_unavailable')
                # Detect configuration drift during the observation window as well.
                final_by_key = {op['key']: op for op in p['plan']['operations']}
                for op in final_by_key.values():
                    if not op.get('irreversible') and adapter.read(op) != op['after']:
                        raise ChangeError('configuration_drift_during_observation')
            if len(readings) < 3 or not all(r.get('target_ok') is True for r in readings[-3:]):
                raise ChangeError('three_consecutive_healthy_readings_required')
            if hasattr(adapter, 'finalize'):
                self.store.event(cid, 'final_commit_receipt', adapter.finalize(p['plan']))
            restore_at = time.time() + p['plan']['restore_after'] if p['plan'].get('restore_after') else None
            self.store.transition(cid, ('observing',), 'succeeded', {'readback': readings[-1], 'restore_at': restore_at}, release=True)
        except Exception as exc:
            self.store.event(cid, 'execution_failed', {'error': str(exc)[:500], 'type': type(exc).__name__})
            if adapter is None:
                self.store.transition(cid, ('executing',), 'needs_human', {'needs_human': True, 'recovery_errors': ['device_connection_failed_before_write']}, release=True)
            else:
                self._recover(p, adapter, attempted, ('executing', 'observing'))
        finally:
            if adapter is not None and hasattr(adapter, 'close'):
                adapter.close()
        return self.store.get(cid)

    def _recover(self, p, adapter, attempted, expected):
        cid = p['id']
        failures = []
        # Reconcile actual values before each inverse. Never overwrite another operator's change.
        originals = {}
        for op in attempted:
            if op['key'] not in originals:
                originals[op['key']] = dict(op, permitted=[])
            originals[op['key']]['permitted'].append(op['after'])
        for op in reversed(list(originals.values())):
            try:
                if op.get('irreversible'):
                    raise ChangeError('transient_action_has_no_inverse')
                actual = adapter.read(op)
                if actual == op['before']:
                    continue
                if actual not in op['permitted']:
                    raise ChangeError('concurrent_configuration_change_requires_operator')
                self.store.event(cid, 'restore_started', {'key': op['key']})
                receipt = adapter.apply(op, op['before'])
                self.store.event(cid, 'restore_receipt', {'key': op['key'], 'receipt': receipt})
                if adapter.read(op) != op['before']:
                    raise ChangeError('restore_readback_failed')
            except Exception as exc:
                failures.append({'key': op['key'], 'error': str(exc)[:300]})
        try:
            observation = adapter.observe(p['plan'])
            self.store.event(cid, 'recovery_health', observation)
            if not observation.get('management_ok') or not observation.get('guard_ok'):
                failures.append({'error': 'recovery_management_or_guard_unhealthy'})
        except Exception:
            failures.append({'error': 'recovery_observation_unavailable'})
        if not failures and hasattr(adapter, 'finalize'):
            try:
                self.store.event(cid, 'recovery_finalized', adapter.finalize(p['plan']))
            except Exception:
                failures.append({'error': 'recovery_finalize_failed'})
        status = 'needs_human' if failures or not attempted else 'rolled_back'
        self.store.transition(cid, expected, status, {'recovery_errors': failures, 'needs_human': status == 'needs_human'}, release=not failures)

    def restore(self, cid: str, actor: str):
        original = self.store.get(cid)
        if not original['plan'].get('manual_restore', True):
            raise ChangeError('action_has_no_meaningful_inverse')
        p = self.store.transition(cid, ('succeeded',), 'restoring', {'restore_actor': actor}, lease=True)
        adapter = None
        try:
            adapter = self.adapter_for(p['device_id'])
            if adapter.identify() != p['identity']:
                raise ChangeError('device_identity_changed')
        except Exception as exc:
            self.store.transition(cid, ('restoring',), 'needs_human', {'recovery_errors': [str(exc)]})
            if adapter is not None and hasattr(adapter, 'close'):
                adapter.close()
            return self.store.get(cid)
        try:
            self._recover(p, adapter, p['plan']['operations'], ('restoring',))
            if self.store.get(cid)['status'] == 'rolled_back':
                self.store.transition(cid, ('rolled_back',), 'restored')
        finally:
            if hasattr(adapter, 'close'):
                adapter.close()
        return self.store.get(cid)

    def reconcile_interrupted(self):
        """Called once during startup. No uncertain device command is replayed."""
        for p in self.store.list(10000):
            if p['status'] in ('executing', 'observing', 'restoring'):
                self.store.transition(p['id'], (p['status'],), 'needs_human',
                    {'needs_human': True, 'recovery_errors': ['process_interrupted_device_state_requires_reconciliation']})

    def expire_temporary_changes(self):
        for p in self.store.list(10000):
            if p['status'] == 'succeeded' and p.get('restore_at') and p['restore_at'] <= time.time():
                try:
                    self.restore(p['id'], 'expiry-worker')
                except ChangeError:
                    pass

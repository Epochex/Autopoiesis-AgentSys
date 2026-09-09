"""Cycle-specific archive receipts and evidence-grounded long-term reflection."""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.memory.store import MemoryClaim, MemoryRecord, TieredMemoryStore


def _now():
    return datetime.now(timezone.utc).isoformat()


def _hash(*parts):
    return hashlib.sha1('|'.join(str(part) for part in parts).encode()).hexdigest()


@contextmanager
def _connection(repository):
    connection = sqlite3.connect(repository.path.parent / 'completed-events.sqlite3', timeout=20)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA synchronous=NORMAL')
        connection.execute('''CREATE TABLE IF NOT EXISTS completed_event_receipts (
            record_id TEXT PRIMARY KEY, snapshot_json TEXT NOT NULL,
            archived_at TEXT, archived_by TEXT, memory_id TEXT, reflected_at TEXT, reflected_by TEXT
        )''')
        connection.execute('''CREATE TABLE IF NOT EXISTS completed_event_audit (
            id INTEGER PRIMARY KEY, record_id TEXT NOT NULL, action TEXT NOT NULL,
            actor TEXT NOT NULL, at TEXT NOT NULL, payload_json TEXT NOT NULL
        )''')
        with connection:
            yield connection
    finally:
        connection.close()


def archived_record_ids(repository) -> set[str]:
    with _connection(repository) as conn:
        rows = [dict(row) for row in conn.execute(
            'SELECT * FROM completed_event_receipts WHERE archived_at IS NOT NULL')]
    hidden = set()
    for row in rows:
        snapshot = json.loads(row['snapshot_json'])
        if snapshot.get('kind') == 'case':
            case = repository.get(row['record_id'])
            if case is None:
                continue
            current = _case_record(case)
            if not current['canArchive'] or current['completedAt'] != snapshot.get('completedAt'):
                continue
        hidden.add(row['record_id'])
    return hidden


def _timeline():
    from core.remediate.sentinel import timeline
    from .demo_execution_trace import demo_execution_events
    events = timeline(20000)
    supplemental = demo_execution_events(existing_events=events)
    # Source clocks use both Z and explicit UTC offsets; sort actual instants.
    def instant(row):
        try:
            value = datetime.fromisoformat(str(row.get('at') or '').replace('Z', '+00:00'))
            return value.replace(tzinfo=value.tzinfo or timezone.utc).timestamp()
        except (ValueError, TypeError):
            return 0.0
    return sorted([*events, *supplemental], key=instant)


def _last(chain, kind):
    return next((row for row in reversed(chain) if row.get('kind') == kind), {})


def _sentinel_records(events):
    from .sentinel_projection import _data_classification
    groups = {}
    for event in events:
        subject = str(event.get('subject') or '')
        if subject:
            groups.setdefault(subject, []).append(event)
    records = []
    for subject, history in groups.items():
        cycles = []
        chain = []
        ended = False
        for event in history:
            if event.get('kind') == 'detected' and ended:
                cycles.append(chain)
                chain = []
                ended = False
            chain.append(event)
            if event.get('kind') in {'resolved', 'condition_cleared', 'escalation_cleared'}:
                ended = True
        if chain:
            cycles.append(chain)
        for chain in cycles:
            detection = _last(chain, 'detected')
            first = next((row for row in chain if row.get('kind') == 'detected'), {})
            if not detection:
                continue
            opened = str(detection.get('cycle_opened_at') or first.get('at') or '')
            resolved = _last(chain, 'resolved')
            remediation = _last(chain, 'remediated')
            cleared = _last(chain, 'condition_cleared') or _last(chain, 'escalation_cleared')
            valid = bool(resolved and resolved.get('outcome') == 'passed'
                         and int(resolved.get('samples') or 0) > 0
                         and resolved.get('target_recovered') is not False
                         and remediation.get('outcome') == 'passed'
                         and not remediation.get('needs_human'))
            if resolved:
                after = chain[chain.index(resolved) + 1:]
                if any(row.get('kind') in {'bakein_regressed', 'revert_unverified', 'escalated',
                                           'remediation_committed', 'bakein_opened'} for row in after):
                    valid = False
                    cleared = {}
            state = 'verified_completed' if valid else 'condition_cleared' if cleared else 'in_progress'
            terminal = resolved if valid else cleared
            scope = _data_classification(subject, detection)
            records.append({
                'recordId': 'sentinel-' + _hash(subject, opened)[:16], 'kind': 'sentinel',
                'subject': subject, 'cycleOpenedAt': opened, 'completedAt': terminal.get('at'),
                'completionState': state, 'canArchive': state != 'in_progress',
                'canReflect': valid, 'scope': scope,
                'problem': json.dumps(detection.get('evidence') or {}, ensure_ascii=False),
                'action': str(remediation.get('action') or resolved.get('action') or ''),
                'preconditions': _last(chain, 'preflight'), 'verification': terminal,
                'sourceTraceIds': ['sentinel:' + _hash(detection.get('detector') or '', subject, opened)[:32],
                    *sorted({prefix + str(row[key]) for row in chain
                             for key, prefix in (('change_id', 'change:'), ('run_id', 'demo:')) if row.get(key)})],
                'evidence': chain,
            })
    return records


def _case_record(case):
    data = case.as_dict()
    decision = data.get('businessDecision') or {}
    readback = decision.get('readback') or {}
    completed = _last(list(case.timeline), 'remediation_completed')
    refs = [row for row in decision.get('evidence') or []
            if row.get('source') == 'action_readback' and row.get('evidenceId')]
    verified = bool(case.status in {'resolved', 'closed'} and readback.get('outcome') == 'passed'
                    and completed.get('outcome') == 'passed' and not completed.get('needsHuman')
                    and (refs or (int(readback.get('samples') or 0) > 0))
                    and readback.get('target_recovered') is not False)
    cleared = bool(case.status in {'resolved', 'closed'} and readback.get('outcome') == 'cleared'
                   and readback.get('observedAt'))
    scope = str(case.source_payload.get('dataClassification') or 'observed')
    if case.subject.startswith(('autopoiesis-acceptance-', 'controlled-', 'replay-', 'synthetic-', 'bvaccept-')):
        scope = 'controlled_test'
    return {'recordId': case.case_id, 'kind': 'case', 'subject': case.subject,
            'cycleOpenedAt': case.first_seen_at, 'completedAt': completed.get('ts') if verified else readback.get('observedAt'),
            'completionState': 'verified_completed' if verified else 'condition_cleared' if cleared else 'in_progress',
            'canArchive': verified or cleared, 'canReflect': verified, 'scope': scope,
            'problem': case.summary, 'action': decision.get('action') or completed.get('action') or '',
            'preconditions': {'service': case.service, 'ruleId': case.rule_id, 'scope': case.scope},
            'verification': readback, 'sourceTraceIds': [case.case_id, *[ref.source_id for ref in case.sources]],
            'evidence': list(case.timeline), 'caseVersion': case.version}


def _public(record, receipt=None):
    receipt = receipt or {}
    if receipt.get('snapshot_json'):
        previous = json.loads(receipt['snapshot_json'])
        if record['kind'] == 'case' and (not record['canArchive'] or record['completedAt'] != previous.get('completedAt')):
            receipt = {}
    fields = ('recordId', 'kind', 'subject', 'cycleOpenedAt', 'completedAt', 'completionState',
              'canArchive', 'canReflect', 'scope')
    return {**{key: record.get(key) for key in fields},
            'archived': bool(receipt.get('archived_at')), 'archivedAt': receipt.get('archived_at'),
            'memoryId': receipt.get('memory_id'), 'reflectedAt': receipt.get('reflected_at'),
            'retrievalReady': bool(receipt.get('memory_id')) and record['scope'] == 'observed',
            'memoryScopeReason': ('Production incident experience; current evidence and action controls still apply.'
                                  if record['scope'] == 'observed' else
                                  'Controlled evidence is retained for inspection and excluded from production retrieval.')}


class CompletedEventService:
    def __init__(self, repository, memory_service=None, timeline_reader=None):
        self.repository = repository
        self.memory_service = memory_service
        self.timeline_reader = timeline_reader or _timeline

    def _records(self):
        records = _sentinel_records(self.timeline_reader())
        records.extend(_case_record(case) for case in self.repository.list(limit=5000))
        return {row['recordId']: row for row in records}

    def list(self):
        records = self._records()
        with _connection(self.repository) as conn:
            receipts = {row['record_id']: dict(row) for row in conn.execute('SELECT * FROM completed_event_receipts')}
        for identity, receipt in receipts.items():
            # Persisted cycle snapshots keep history inspectable after the tail rotates.
            records.setdefault(identity, json.loads(receipt['snapshot_json']))
        return {'ok': True, 'records': [_public(row, receipts.get(identity)) for identity, row in records.items()]}

    def _load(self, record_id, conn):
        record = self._records().get(record_id)
        receipt = conn.execute('SELECT * FROM completed_event_receipts WHERE record_id=?', (record_id,)).fetchone()
        if record is None and receipt is not None:
            record = json.loads(receipt['snapshot_json'])
        if record is None:
            raise HTTPException(404, 'Completed event record not found')
        receipt = dict(receipt) if receipt else {}
        if receipt and record['kind'] == 'case':
            previous = json.loads(receipt['snapshot_json'])
            if not record['canArchive'] or record['completedAt'] != previous.get('completedAt'):
                conn.execute('''UPDATE completed_event_receipts SET snapshot_json=?,
                    archived_at=NULL,archived_by=NULL,memory_id=NULL,reflected_at=NULL,reflected_by=NULL WHERE record_id=?''',
                    (json.dumps(record, ensure_ascii=False), record_id))
                receipt = {}
        return record, receipt

    def archive(self, record_id, actor):
        with _connection(self.repository) as conn:
            conn.execute('BEGIN IMMEDIATE')
            record, receipt = self._load(record_id, conn)
            if not record['canArchive']:
                raise HTTPException(409, 'Only an event with verified completion or observed condition clearance can be removed')
            if not receipt.get('archived_at'):
                at = _now()
                conn.execute('''INSERT INTO completed_event_receipts(record_id,snapshot_json,archived_at,archived_by)
                    VALUES(?,?,?,?) ON CONFLICT(record_id) DO UPDATE SET
                    archived_at=excluded.archived_at, archived_by=excluded.archived_by,snapshot_json=excluded.snapshot_json''',
                    (record_id, json.dumps(record, ensure_ascii=False), at, actor))
                conn.execute('INSERT INTO completed_event_audit(record_id,action,actor,at,payload_json) VALUES(?,?,?,?,?)',
                             (record_id, 'archived', actor, at, json.dumps({'completionState': record['completionState']})))
                receipt.update(archived_at=at)
            return {'ok': True, 'record': _public(record, receipt)}

    def reflect(self, record_id, actor):
        service = self.memory_service
        memory = getattr(service, 'memory', None)
        if not isinstance(memory, TieredMemoryStore) or memory.repository is None:
            raise HTTPException(503, 'Durable investigation memory is unavailable; reflection was not saved')
        lock = getattr(service, '_request_lock', None)
        with lock if lock is not None else nullcontext():
            with _connection(self.repository) as conn:
                conn.execute('BEGIN IMMEDIATE')
                record, receipt = self._load(record_id, conn)
                if not record['canReflect']:
                    raise HTTPException(409, 'Reflection as a successful experience requires an executed action and verified readback')
                memory_id = 'reflection-' + _hash(record_id, record['cycleOpenedAt'], record['completedAt'])[:24]
                # Read durable truth, including a previous write whose receipt failed.
                durable = next((row for row in memory.repository.load_records(include_quarantined=True)
                                if row.memory_id == memory_id), None)
                if durable is None:
                    durable = self._memory_record(record, memory_id)
                    before = [item.model_copy(deep=True) for item in memory.records()]
                    try:
                        memory.add(durable)
                        memory.flush()
                        saved = next((row for row in memory.repository.load_records(include_quarantined=True)
                                      if row.memory_id == memory_id), None)
                        if saved is None:
                            raise RuntimeError('durable readback did not contain reflection')
                    except Exception as error:
                        memory.replace_records(before)
                        raise HTTPException(503, f'Reflection persistence failed: {type(error).__name__}') from error
                elif memory.get(memory_id) is None:
                    # Flush pending work before synchronizing another worker's committed reflection.
                    memory.flush()
                    memory.reload_from_repository()
                if not receipt.get('memory_id'):
                    at = _now()
                    conn.execute('''INSERT INTO completed_event_receipts(record_id,snapshot_json,memory_id,reflected_at,reflected_by)
                        VALUES(?,?,?,?,?) ON CONFLICT(record_id) DO UPDATE SET memory_id=excluded.memory_id,
                        reflected_at=excluded.reflected_at,reflected_by=excluded.reflected_by,snapshot_json=excluded.snapshot_json''',
                        (record_id, json.dumps(record, ensure_ascii=False), memory_id, at, actor))
                    conn.execute('INSERT INTO completed_event_audit(record_id,action,actor,at,payload_json) VALUES(?,?,?,?,?)',
                                 (record_id, 'reflected', actor, at, json.dumps({'memoryId': memory_id, 'scope': record['scope']})))
                    receipt.update(memory_id=memory_id, reflected_at=at)
                return {'ok': True, 'record': _public(record, receipt), 'memoryId': memory_id,
                        'scope': record['scope'], 'retrievalReady': not durable.quarantined and durable.retrieval_ready}

    @staticmethod
    def _memory_record(record, memory_id):
        controlled = record['scope'] != 'observed'
        evidence = record['evidence']
        evidence_ids = ['reflection-evidence-' + _hash(record['recordId'], json.dumps(row, sort_keys=True))[:20]
                        for row in evidence]
        applicability = (f"scope={record['scope']}; subject={record['subject']}; cycle={record['cycleOpenedAt']}; "
                         'Reuse requires matching conditions, current readback, action preconditions and operator authorization.')
        components = {
            'symptom': record['problem'],
            'action': json.dumps({'action': record['action'], 'commandReceipts': [
                row for row in evidence if row.get('kind') == 'command'
            ]}, ensure_ascii=False, sort_keys=True),
            'outcome': json.dumps({'summary': record['verification'], 'readbackSamples': [
                row for row in evidence if row.get('kind') in {'business_readback', 'bakein_sampled'}
            ]}, ensure_ascii=False, sort_keys=True),
            'applicability': applicability,
            'constraint': json.dumps(record['preconditions'], ensure_ascii=False, sort_keys=True),
        }
        return MemoryRecord(
            memory_id=memory_id, tier='episodic',
            text='\n'.join(f'{key}: {value}' for key, value in components.items()),
            tags=['operator_reflection', 'verified_completion', f"scope:{record['scope']}",
                  *(['quarantine:controlled_scope'] if controlled else [])],
            asset_ids=[record['subject']], evidence_ids=evidence_ids,
            source_trace_ids=record['sourceTraceIds'],
            evidence_snapshot=[{'evidence_id': identity, 'source': record['recordId'], 'payload': row}
                               for identity, row in zip(evidence_ids, evidence)],
            claims=[MemoryClaim(kind=key, statement=str(value), evidence_ids=evidence_ids)
                    for key, value in components.items()],
            quarantined=controlled, retrieval_ready=not controlled,
            first_observed_at=record['cycleOpenedAt'] or None,
            last_observed_at=record['completedAt'] or None,
            event_type='verified_incident_reflection',
        )


class CompletedEventRequest(BaseModel):
    recordId: str = Field(min_length=1, max_length=160)
    actor: str = Field(default='operator', min_length=1, max_length=120)


def create_router(case_repository_getter: Callable, memory_service_getter: Callable) -> APIRouter:
    router = APIRouter(prefix='/api/rca/completed-events')
    def service():
        return CompletedEventService(case_repository_getter(), memory_service_getter())

    @router.get('')
    def list_completed_events():
        return service().list()

    @router.post('/archive')
    def archive_completed_event(body: CompletedEventRequest, request: Request):
        from .change_routes import actor
        return service().archive(body.recordId, actor(request))

    @router.post('/reflect')
    def reflect_completed_event(body: CompletedEventRequest, request: Request):
        from .change_routes import actor
        return service().reflect(body.recordId, actor(request))

    return router

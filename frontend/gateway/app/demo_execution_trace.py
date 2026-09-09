"""Read-only projection of exact controlled-demo change receipts."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3


def project_change_event(event, subject, *, include_operation_command=True):
    payload = event.get('payload') or {}
    common = {'subject': subject, 'action': 'restart_unit', 'change_id': event['change_id'],
              'source': 'device_changes', 'source_event_seq': event['seq'],
              'at': datetime.fromtimestamp(float(event['at']), timezone.utc).isoformat()}
    kind = event['kind']
    if kind == 'command':
        return [{**payload, **common, 'kind': 'command'}]
    if kind == 'operation_receipt':
        receipt = payload.get('receipt') or {}
        rows = [{**common, 'kind': 'remediation_committed', 'receipt': receipt}]
        if include_operation_command and isinstance(receipt.get('command'), list):
            rows.append({**common, 'kind': 'command', 'phase': 'receipt',
                         'argv': receipt['command'], 'rc': None, 'stdout': None, 'stderr': None,
                         'out': json.dumps(receipt, ensure_ascii=False), 'receipt': receipt,
                         'purpose': 'Recorded operation receipt; process exit code and output were not captured',
                         'truncated': False})
        return rows
    if kind == 'business_readback':
        keys = ('management_ok', 'guard_ok', 'target_ok')
        return [{**common, 'kind': 'bakein_sampled', 'phase': 'verification',
                 'probe': 'service_and_management_gateway', 'reading': payload,
                 'healthy': all(payload.get(key) is True for key in keys),
                 'available': all(key in payload for key in keys)}]
    if kind == 'observing':
        return [{**common, 'kind': 'bakein_opened'}]
    if kind == 'execution_failed':
        return [{**common, 'kind': 'execution_error', 'stage': 'execution',
                 'error': payload.get('error'), 'needs_human': True, 'outcome': 'execution_error'}]
    return []


def demo_execution_events(subject=None, *, existing_events=(), runs=None, db_path=None):
    """Join an exact run/subject/change triple without editing either source ledger."""
    from .demo_scenarios import recent_runs
    runs = recent_runs(2000) if runs is None else runs
    path = Path(db_path or os.getenv('AUTOPOIESIS_CHANGE_DB', '/data/autopoiesis-production/device-changes.sqlite3'))
    if not path.exists():
        return []
    result = []
    seen = set()
    try:
        with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            for run in runs:
                cid = run.get('changeId') or (run.get('metrics') or {}).get('changeId')
                target = run.get('subject')
                if not cid or cid in seen or (subject and target != subject) or run.get('scenario') != 'self_healing':
                    continue
                seen.add(cid)
                row = db.execute('SELECT payload FROM changes WHERE id=?', (cid,)).fetchone()
                if row is None:
                    continue
                plan = json.loads(row['payload'])
                if plan.get('target', {}).get('unit') != target or plan.get('action') != 'restart_unit':
                    continue
                events = [{**dict(row), 'payload': json.loads(row['payload'])} for row in db.execute(
                    'SELECT seq,change_id,at,kind,payload FROM events WHERE change_id=? ORDER BY seq', (cid,))]
                if not any(e['kind'] == 'controlled_demo_scope' and e['payload'].get('unit') == target
                           and e['payload'].get('run_id') == run.get('runId') for e in events):
                    continue
                has_commands = any(e['kind'] == 'command' and e['payload'].get('command_id') for e in events)
                has_commands |= any(e.get('change_id') == cid and e.get('command_id') for e in existing_events)
                for event in events:
                    result.extend({**row, 'run_id': run.get('runId')} for row in
                                  project_change_event(event, target, include_operation_command=not has_commands))
    except (sqlite3.Error, ValueError, TypeError):
        return []
    return sorted(result, key=lambda row: (row['at'], row['source_event_seq']))

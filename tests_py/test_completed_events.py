from copy import deepcopy
import json
from threading import RLock
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from core.memory.store import MemoryRecord, TieredMemoryStore
from domains.network_rca.investigation_case import InvestigationCaseRepository, CaseObservation, SourceReference
from frontend.gateway.app.completed_events import CompletedEventService, archived_record_ids, create_router


class DurableMemory:
    """File-backed implementation of the same memory repository protocol."""
    def __init__(self, path):
        self.path = path
        self.writes = 0

    def load_records(self, **kwargs):
        return [MemoryRecord.model_validate(row) for row in json.loads(self.path.read_text())] if self.path.exists() else []

    def sync_records(self, records):
        self.writes += 1
        self.path.write_text(json.dumps([row.model_dump(mode='json') for row in records]))
        return []


def chain(subject='collector.service', offset='10'):
    at = f'2026-09-08T{offset}:00:'
    base = {'subject': subject, 'detector': 'failed_units'}
    return [
        {**base, 'kind': 'detected', 'at': at + '00Z', 'evidence': {'ActiveState': 'failed'}},
        {**base, 'kind': 'preflight', 'at': at + '01Z', 'action': 'restart_unit', 'eligible': True},
        {**base, 'kind': 'command', 'at': at + '02Z', 'argv': ['systemctl', 'restart', subject], 'rc': 0, 'out': ''},
        {**base, 'kind': 'remediated', 'at': at + '03Z', 'action': 'restart_unit', 'outcome': 'passed', 'samples': 3},
        {**base, 'kind': 'resolved', 'at': at + '04Z', 'action': 'restart_unit', 'outcome': 'passed', 'samples': 3,
         'target_recovered': True},
    ]


@pytest.fixture
def state(tmp_path):
    cases = InvestigationCaseRepository(tmp_path / 'cases.sqlite3')
    durable = DurableMemory(tmp_path / 'memory.json')
    memory = TieredMemoryStore(repository=durable)
    events = chain()
    service = CompletedEventService(cases, SimpleNamespace(memory=memory, _request_lock=RLock()), lambda: events)
    return service, cases, memory, durable, events


def test_archive_is_durable_idempotent_and_retains_audit(state):
    service, cases, _, _, events = state
    row = service.list()['records'][0]
    result = service.archive(row['recordId'], 'operator')
    assert result['record']['archived']
    assert row['recordId'] in archived_record_ids(cases)
    assert service.archive(row['recordId'], 'operator')['record']['archivedAt'] == result['record']['archivedAt']
    events.clear()
    restored = CompletedEventService(cases, timeline_reader=lambda: []).list()['records'][0]
    assert restored['archived'] and restored['completionState'] == 'verified_completed'
    import sqlite3
    with sqlite3.connect(cases.path.parent / 'completed-events.sqlite3') as conn:
        assert conn.execute('SELECT count(*) FROM completed_event_audit').fetchone()[0] == 1


def test_same_subject_recurrence_is_visible_as_a_new_cycle(state):
    service, cases, _, _, events = state
    first = service.list()['records'][0]
    service.archive(first['recordId'], 'operator')
    events.extend(chain(offset='11')[:2])
    rows = service.list()['records']
    assert len(rows) == 2
    assert rows[0]['archived'] and not rows[1]['archived']
    assert rows[1]['recordId'] not in archived_record_ids(cases)
    assert not rows[1]['canArchive'] and not rows[1]['canReflect']


@pytest.mark.parametrize('failure', ['no_resolution', 'no_samples', 'target_failed', 'regressed'])
def test_unverified_events_cannot_archive_or_become_success_memory(state, failure):
    service, _, _, _, events = state
    if failure == 'no_resolution':
        events.pop()
    elif failure == 'no_samples':
        events[-1]['samples'] = 0
    elif failure == 'target_failed':
        events[-1]['target_recovered'] = False
    else:
        events.append({**events[-1], 'kind': 'bakein_regressed', 'at': '2026-09-08T10:00:05Z'})
    row = service.list()['records'][0]
    for operation in (service.archive, service.reflect):
        with pytest.raises(HTTPException) as error:
            operation(row['recordId'], 'operator')
        assert error.value.status_code == 409


def test_condition_cleared_can_be_removed_without_claiming_executed_recovery(state):
    service, _, _, _, events = state
    events[:] = [events[0], {'kind': 'condition_cleared', 'subject': 'collector.service',
                           'at': '2026-09-08T10:00:05Z', 'note': 'detector condition absent'}]
    row = service.list()['records'][0]
    assert row['completionState'] == 'condition_cleared'
    assert row['canArchive'] and not row['canReflect']
    service.archive(row['recordId'], 'operator')
    with pytest.raises(HTTPException):
        service.reflect(row['recordId'], 'operator')


def test_reflection_is_durable_retrievable_and_idempotent(state):
    service, _, memory, durable, _ = state
    row = service.list()['records'][0]
    result = service.reflect(row['recordId'], 'operator')
    assert result['retrievalReady'] and result['scope'] == 'observed'
    record = memory.get(result['memoryId'])
    assert record and record.evidence_ids and record.source_trace_ids
    assert {claim.kind for claim in record.claims} == {'symptom', 'action', 'outcome', 'applicability', 'constraint'}
    assert record.memory_id in [item.memory_id for item in memory.retrieve(['collector.service'], ['collector.service'])['episodic']]
    restored = TieredMemoryStore.from_repository(durable)
    assert restored.get(record.memory_id)
    assert service.reflect(row['recordId'], 'operator')['memoryId'] == result['memoryId']
    assert durable.writes == 1


def test_controlled_reflection_is_inspectable_and_excluded_from_production_retrieval(state):
    service, _, memory, _, events = state
    events[:] = chain(subject='autopoiesis-acceptance-memory.service')
    result = service.reflect(service.list()['records'][0]['recordId'], 'operator')
    assert result['scope'] == 'controlled_test' and not result['retrievalReady']
    record = memory.get(result['memoryId'])
    assert record.quarantined and 'scope:controlled_test' in record.tags
    assert not memory.retrieve(['autopoiesis-acceptance-memory.service'], record.asset_ids)['episodic']


def test_failed_memory_write_is_explicit_and_does_not_leave_local_success(state, monkeypatch):
    service, _, memory, durable, _ = state
    def fail(*args):
        raise OSError('storage unavailable')
    monkeypatch.setattr(durable, 'sync_records', fail)
    with pytest.raises(HTTPException) as error:
        service.reflect(service.list()['records'][0]['recordId'], 'operator')
    assert error.value.status_code == 503
    assert memory.records() == []
    assert service.list()['records'][0]['memoryId'] is None


def test_memory_unavailable_does_not_fall_back_to_isolated_json(state):
    service, _, _, _, _ = state
    service.memory_service = None
    with pytest.raises(HTTPException) as error:
        service.reflect(service.list()['records'][0]['recordId'], 'operator')
    assert error.value.status_code == 503


def test_case_needs_real_readback_and_reopen_is_not_hidden(state):
    service, cases, _, _, events = state
    events.clear()
    case = cases.ingest(CaseObservation(source=SourceReference('alert', 'a1'),
        occurred_at='2026-09-08T10:00:00Z', subject='192.168.1.10', summary='address conflict'))
    cases.append_event(case.case_id, kind='business_decision_recorded', status='resolved',
        payload={'decision': {'state': 'resolved', 'readback': {'outcome': 'passed'}}})
    assert not service.list()['records'][0]['canArchive']
    cases.append_event(case.case_id, kind='remediation_completed', status='resolved',
        payload={'outcome': 'passed', 'readbackEvidenceId': 'rb-1', 'action': 'restart_unit'})
    cases.append_event(case.case_id, kind='business_decision_recorded', status='resolved',
        payload={'decision': {'state': 'resolved', 'readback': {'outcome': 'passed'},
            'evidence': [{'source': 'action_readback', 'evidenceId': 'rb-1'}]}})
    assert service.list()['records'][0]['canReflect']
    service.archive(case.case_id, 'operator')
    assert case.case_id in archived_record_ids(cases)
    cases.append_event(case.case_id, kind='case_reopened', status='investigating')
    assert case.case_id not in archived_record_ids(cases)
    assert not service.list()['records'][0]['archived']


def test_routes_default_actor_and_unknown_record_errors(state, monkeypatch):
    service, cases, _, _, events = state
    monkeypatch.setattr('frontend.gateway.app.completed_events._timeline', lambda: events)
    app = FastAPI()
    app.include_router(create_router(lambda: cases, lambda: service.memory_service))
    client = TestClient(app, headers={'X-Autopoiesis-Console': 'change-control'})
    record_id = client.get('/api/rca/completed-events').json()['records'][0]['recordId']
    assert client.post('/api/rca/completed-events/archive', json={'recordId': record_id}).status_code == 200
    assert client.post('/api/rca/completed-events/reflect', json={'recordId': record_id}).json()['memoryId']
    assert client.post('/api/rca/completed-events/archive', json={'recordId': 'missing'}).status_code == 404


def test_cross_origin_and_bare_posts_are_rejected(state):
    service, cases, _, _, _ = state
    app = FastAPI()
    app.include_router(create_router(lambda: cases, lambda: service.memory_service))
    client = TestClient(app)
    body = {'recordId': 'missing'}
    assert client.post('/api/rca/completed-events/archive', json=body).status_code == 403
    assert client.post('/api/rca/completed-events/reflect', json=body, headers={
        'X-Autopoiesis-Console': 'change-control', 'Origin': 'https://external.example',
    }).status_code == 403


def test_supplemental_change_receipts_enrich_memory_without_changing_cycle_identity(state):
    service, _, memory, _, events = state
    original = service.list()['records'][0]
    service.archive(original['recordId'], 'operator')
    events[3:3] = [
        {'subject': 'collector.service', 'kind': 'command', 'at': '2026-09-08T10:00:02.1Z',
         'argv': ['systemctl', 'is-active', 'collector.service'], 'rc': 0, 'out': 'active',
         'sourceRunId': 'demo-run-1', 'sourceChangeId': 'change-1'},
        {'subject': 'collector.service', 'kind': 'business_readback', 'at': '2026-09-08T10:00:02.2Z',
         'target_ok': True, 'guard_ok': True, 'sourceRunId': 'demo-run-1', 'sourceChangeId': 'change-1'},
    ]
    current = service.list()['records'][0]
    assert current['recordId'] == original['recordId'] and current['cycleOpenedAt'] == original['cycleOpenedAt']
    assert current['archived'] and current['canReflect']
    result = service.reflect(current['recordId'], 'operator')
    saved = memory.get(result['memoryId'])
    assert 'systemctl' in saved.text and 'is-active' in saved.text and 'guard_ok' in saved.text
    samples = [row['payload'] for row in saved.evidence_snapshot if row['payload']['kind'] == 'business_readback']
    assert samples[0]['sourceChangeId'] == 'change-1' and samples[0]['guard_ok'] is True


def test_timeline_reads_exact_demo_receipts_in_time_order(monkeypatch):
    from frontend.gateway.app import completed_events
    original = chain()
    receipt = {'subject': 'collector.service', 'kind': 'bakein_sampled',
               'at': '2026-09-08T10:00:02.500000+00:00', 'reading': {'target_ok': True},
               'change_id': 'change-1', 'run_id': 'run-1'}
    monkeypatch.setattr('core.remediate.sentinel.timeline', lambda limit: original)
    seen = []
    def supplement(**kwargs):
        seen.append(kwargs['existing_events'])
        return [receipt]
    monkeypatch.setattr('frontend.gateway.app.demo_execution_trace.demo_execution_events', supplement)
    events = completed_events._timeline()
    assert seen == [original] and events.index(receipt) < events.index(original[-1])
    record = completed_events._sentinel_records(events)[0]
    assert record['canReflect']
    assert 'change:change-1' in record['sourceTraceIds'] and 'demo:run-1' in record['sourceTraceIds']
    assert record['recordId'] == completed_events._sentinel_records(original)[0]['recordId']

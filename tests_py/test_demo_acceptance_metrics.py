import subprocess

import pytest

from scripts.demo_memory_trajectory import indexed_procedure_id, measured_comparison, ordered_trace
from scripts import demo_support
from domains.network_rca.investigation_case import InvestigationCaseRepository
from frontend.gateway.app.demo_scenarios import append_run, scenario_snapshot


def test_actual_work_is_distinct_from_shadow_candidates():
    baseline = {'memory_intervention': {'executed_order':['a','b']}}
    warm = {'memory_intervention': {'executed_order':['a'], 'no_memory_shadow_order':['a','b','c','d'], 'saved_probe_count':3}}
    result = measured_comparison(baseline, warm)
    assert result['actualSavedProbes'] == 1
    assert result['shadowSavedProbes'] == 3
    assert result['baselineExecutedProbes'] == 2


def test_memory_demo_binds_recall_to_the_first_pass_and_checks_trace_order():
    session = {'trace_events': [
        {'kind': 'working_set_updated', 'payload': {}},
        {'kind': 'verified_procedure_indexed', 'payload': {'memory_id': 'proc:case-1'}},
    ]}
    assert indexed_procedure_id(session) == 'proc:case-1'
    trace = [{'kind': kind} for kind in (
        'memory_candidates_ranked', 'hypothesis_confirmed', 'memory_shortcut',
        'memory_intervention_evaluated', 'business_decision_recorded',
    )]
    assert ordered_trace(trace, [row['kind'] for row in trace]) is True
    assert ordered_trace(trace, ['memory_shortcut', 'hypothesis_confirmed']) is False


def test_existing_unit_is_never_overwritten(monkeypatch):
    monkeypatch.setattr(demo_support, 'run_command', lambda *a, **k: subprocess.CompletedProcess([],0,'loaded\n',''))
    with pytest.raises(RuntimeError, match='Refusing to replace'):
        demo_support.install_demo_unit('autopoiesis-acceptance-existing.service', 'unrelated')


def test_running_receipt_is_visible_and_then_replaced_by_terminal(monkeypatch,tmp_path):
    monkeypatch.setenv('AUTOPOIESIS_DEMO_RUNS_PATH',str(tmp_path/'runs.jsonl'))
    common = dict(runId='test-1',scenario='self_healing',startedAt='2026-09-07',completedAt='2026-09-07',subject='test-unit',headline='preview',changeId='c1')
    append_run(dict(common,status='running'))
    assert scenario_snapshot()['latest']['self_healing']['status']=='running'
    append_run(dict(common,status='passed'))
    assert scenario_snapshot()['latest']['self_healing']['status']=='passed'


def test_controlled_demo_uses_a_real_case_identity_and_keeps_its_source_boundary(tmp_path):
    repository = InvestigationCaseRepository(tmp_path / 'cases.sqlite3')

    case_id = demo_support.create_demo_case(
        repository,
        run_id='selfheal-one',
        scenario='self_healing',
        subject='autopoiesis-acceptance-selfheal-one.service',
        summary='专用服务连续两次检测失败。',
    )

    case = repository.get(case_id)
    assert case_id.startswith('case-')
    assert case is not None
    assert case.source_payload['dataClassification'] == 'controlled_test'
    assert case.source_payload['demoRunId'] == 'selfheal-one'
    assert case.source_payload['demoScenario'] == 'self_healing'

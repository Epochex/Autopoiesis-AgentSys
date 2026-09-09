import subprocess

import pytest

from scripts.demo_memory_trajectory import measured_comparison
from scripts import demo_support
from frontend.gateway.app.demo_scenarios import append_run, scenario_snapshot


def test_actual_work_is_distinct_from_shadow_candidates():
    baseline = {'memory_intervention': {'executed_order':['a','b']}}
    warm = {'memory_intervention': {'executed_order':['a'], 'no_memory_shadow_order':['a','b','c','d'], 'saved_probe_count':3}}
    result = measured_comparison(baseline, warm)
    assert result['actualSavedProbes'] == 1
    assert result['shadowSavedProbes'] == 3
    assert result['baselineExecutedProbes'] == 2


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

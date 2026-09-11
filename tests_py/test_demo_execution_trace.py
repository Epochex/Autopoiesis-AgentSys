from contextlib import nullcontext
import json
import subprocess
from types import SimpleNamespace

import pytest

from core.remediate.change_control import ChangeStore
from frontend.gateway.app.demo_execution_trace import demo_execution_events


def test_store_observer_runs_after_commit_and_never_on_rollback(tmp_path):
    observed = []
    store = ChangeStore(tmp_path / 'changes.sqlite', on_event=lambda row: observed.append(row))
    store.event('test', 'command', {'argv': ['test'], 'phase': 'started'})
    assert observed[0]['seq'] == 1
    with store.db() as db:
        assert db.execute('SELECT count(*) FROM events').fetchone()[0] == 1
    with pytest.raises(ValueError):
        with store.db() as db:
            store.event('test', 'command', {}, db=db)
            assert len(observed) == 1
            raise ValueError('rollback')
    assert len(observed) == 1
    with store.db() as db:
        assert db.execute('SELECT count(*) FROM events').fetchone()[0] == 1


def test_exact_demo_join_preserves_seven_readings_and_unknown_process_fields(tmp_path):
    path = tmp_path / 'changes.sqlite'
    store = ChangeStore(path)
    store.create({'id': 'c1', 'status': 'succeeded', 'digest': 'a', 'actor': 'demo', 'action': 'restart_unit', 'target': {'unit': 'demo.service'}})
    store.event('c1', 'controlled_demo_scope', {'unit': 'demo.service', 'run_id': 'run-one'})
    store.event('c1', 'operation_receipt', {'receipt': {'command': ['systemctl', 'restart', 'demo.service'], 'immediate_ok': True}})
    for _ in range(7):
        store.event('c1', 'business_readback', {'target_ok': True, 'guard_ok': True, 'management_ok': True})
    run = {'runId': 'run-one', 'subject': 'demo.service', 'changeId': 'c1', 'scenario': 'self_healing'}
    rows = demo_execution_events(runs=[run], db_path=path)
    assert len([r for r in rows if r['kind'] == 'bakein_sampled']) == 7
    command = next(r for r in rows if r['kind'] == 'command')
    assert command['phase'] == 'receipt' and command['rc'] is None
    assert command['stdout'] is None and command['stderr'] is None
    assert all(r['change_id'] == 'c1' and r['run_id'] == 'run-one' and r['source_event_seq'] for r in rows)
    assert not any(r['kind'] in ('detected', 'resolved') for r in rows)
    assert demo_execution_events(runs=[{**run, 'subject': 'other.service'}], db_path=path) == []
    assert demo_execution_events(runs=[{**run, 'runId': 'wrong-run'}], db_path=path) == []
    primary = [{'change_id': 'c1', 'command_id': 'actual-command', 'kind': 'command'}]
    assert not any(r['kind'] == 'command' for r in demo_execution_events(runs=[run], db_path=path, existing_events=primary))


def test_actual_self_healing_script_streams_mock_host_commands_and_samples(tmp_path, monkeypatch):
    from scripts import demo_self_healing as script
    from frontend.gateway.app.host_changes import HostAdapter
    from domains.network_rca import detectors
    from domains.network_rca import remediation as network_remediation
    from core.remediate.sentinel import Detection, timeline
    from pathlib import Path
    import socket
    monkeypatch.setenv('AUTOPOIESIS_CHANGE_DB', str(tmp_path / 'changes.sqlite'))
    monkeypatch.setenv('AUTOPOIESIS_SENTINEL_TIMELINE', str(tmp_path / 'timeline.jsonl'))
    monkeypatch.setenv('AUTOPOIESIS_DEMO_RUNS_PATH', str(tmp_path / 'runs.jsonl'))
    monkeypatch.setattr(script, 'Path', lambda value: tmp_path / 'run' if value == '/run' else tmp_path / 'systemd' if value == '/run/systemd/system' else Path(value))
    (tmp_path / 'run').mkdir()
    host = {'unit': '', 'state': 'failed'}
    monkeypatch.setattr(script, 'demo_options', lambda _: SimpleNamespace(test=True))
    monkeypatch.setattr(script, 'install_demo_unit', lambda unit, _: host.update(unit=unit))
    monkeypatch.setattr(script, 'run_command', lambda *args, **kwargs: None)
    monkeypatch.setattr(script, 'wait_unit_state', lambda *args: None)
    monkeypatch.setattr(script, 'remove_demo_unit', lambda *args: None)
    monkeypatch.setattr(script, 'stage', lambda *args, **kwargs: None)
    monkeypatch.setattr(script, 'print_receipt', lambda *args: None)
    monkeypatch.setattr(socket, 'create_connection', lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(detectors, 'failed_units', lambda **_: [Detection(detector='failed_units', family='test', subject=host['unit'], target=host['unit'], severity='high', summary='mock failed service', action='restart_unit')] if host['state'] == 'failed' else [])
    monkeypatch.setattr(network_remediation, 'read_unit', lambda _command, unit: {
        'unit': unit, 'active': host['state'], 'running': host['state'] == 'active',
        'failed': host['state'] == 'failed', 'restarts': 0,
    })
    def fake_run(self, argv):
        if argv[:2] == ['systemctl', 'restart']:
            host['state'] = 'active'
            output = ''
        elif argv[:2] == ['systemctl', 'is-active']:
            output = host['state']
        elif argv[0] == 'cat':
            output = 'a' * 32
        elif argv[0] == 'hostname':
            output = 'mock-host'
        else:
            output = '0' if argv[:2] == ['systemctl', 'show'] else ''
        return subprocess.CompletedProcess(argv, 0, output, '')
    monkeypatch.setattr(HostAdapter, 'run', fake_run)
    assert script.main() == 0
    rows = timeline(2000)
    commands = [r for r in rows if r['kind'] == 'command']
    restart = [r for r in commands if r['argv'][:2] == ['systemctl', 'restart']]
    assert [r['phase'] for r in restart] == ['started', 'completed']
    assert restart[0]['command_id'] == restart[1]['command_id']
    assert restart[1]['rc'] == 0 and restart[1]['source_event_seq']
    samples = [r for r in rows if r['kind'] == 'bakein_sampled']
    assert len(samples) == 4 and all(r['healthy'] for r in samples)
    resolved = next(r for r in rows if r['kind'] == 'resolved')
    assert resolved['samples'] == 4 and resolved['execution_id'] == restart[1]['change_id']
    # The old-record join recognizes new primary command receipts, so it cannot
    # introduce a second synthetic-looking restart alongside the real one.
    supplemental = demo_execution_events(db_path=tmp_path / 'changes.sqlite', existing_events=rows)
    assert not any(r.get('phase') == 'receipt' for r in supplemental)
    assert any(r['kind'] == 'bakein_sampled' for r in supplemental)

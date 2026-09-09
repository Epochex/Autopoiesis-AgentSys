#!/usr/bin/env python3
"""Real failed-service recovery through the approved change pipeline."""
from __future__ import annotations
import os
import sys
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from scripts.demo_support import (demo_options, install_demo_unit, print_receipt,
    remove_demo_unit, run_command, stage, utc_now, wait_unit_state)


def main():
    args = demo_options(__doc__)
    from core.remediate.change_control import ChangeService, ChangeStore, ChangeError, digest
    from core.remediate.sentinel import Sentinel, record
    from domains.network_rca.detectors import failed_units
    from frontend.gateway.app.host_changes import HostAdapter
    from frontend.gateway.app.demo_scenarios import append_run
    from frontend.gateway.app.demo_execution_trace import project_change_event
    from domains.network_rca.remediation import Command, CommandLog

    run_id = 'selfheal-' + uuid.uuid4().hex[:12]
    unit = 'autopoiesis-acceptance-' + run_id + '.service'
    marker = Path('/run') / (unit + '.fail-once')
    unit_path = Path('/run/systemd/system') / unit
    device_id = 'controlled-' + run_id
    case_scope = 'controlled:' + run_id
    actor = 'controlled-demo:' + run_id
    started = utc_now()
    installed = False
    marker_created = False
    result = {}
    plan_id = None
    def stream_change(event):
        for row in project_change_event(event, unit, include_operation_command=False):
            record(row['kind'], {**row, 'run_id': run_id})
    store = ChangeStore(os.getenv('AUTOPOIESIS_CHANGE_DB', '/data/autopoiesis-production/device-changes.sqlite3'), on_event=stream_change)
    command_sink = None
    def stream_command(entry):
        entry = {**entry, 'run_id': run_id}
        if plan_id:
            store.event(plan_id, 'command', entry)
        elif command_sink is not None:
            command_sink(entry)
    # Explicit per-invocation authority, confined to one newly-created local unit.
    # No production policy, router adapter or global write switch is changed.
    host_policy = {'id': device_id, 'units': [unit], 'business_checks': [
        {'host': '127.0.0.1', 'port': 8026, 'reachable': True}]}
    class DemoHost(HostAdapter):
        def __init__(self, policy):
            super().__init__(policy)
            self.command_log = CommandLog(on_entry=stream_command)
            self.observed_command = Command(super().run).with_log(self.command_log)

        def run(self, argv):
            return self.observed_command.run(argv)

        def apply(self, op, value):
            self.command_log.stage = 'execution'
            return super().apply(op, value)

        def observe(self, plan):
            self.command_log.stage = 'verification'
            import socket
            result = super().observe(dict(plan, business_checks=[]))
            try:
                with socket.create_connection(('127.0.0.1', 8026), timeout=3):
                    reachable = True
            except OSError:
                reachable = False
            result.update(guard_ok=reachable, checks=[{'host':'127.0.0.1',
                'port':8026, 'reachable':True, 'passed':reachable}])
            return result

        def prepare(self, action, target, params):
            p = super().prepare(action, target, params)
            p.update(observation_interval=1 if args.test else 5,
                     observation_samples=4 if args.test else 7,
                     verification_scope='专用测试服务状态与本机网关连接，持续真实回读')
            return p
    def adapter_for(device):
        if device != device_id:
            raise ChangeError('controlled_demo_target_mismatch')
        return DemoHost(host_policy)
    stop = Path('/data/autopoiesis-production/demo/emergency-stop.json')
    def paused():
        import json
        if not stop.exists():
            return False
        try:
            return bool(json.loads(stop.read_text()).get('paused', True))
        except Exception:
            return True
    svc = ChangeService(store, adapter_for, paused=paused)
    def receipt(status, phase, metrics=None):
        return append_run(dict(runId=run_id, scenario='self_healing', status=status,
            startedAt=started, completedAt=utc_now(), subject=unit,
            headline=phase, phase=phase, caseId=case_scope, changeId=plan_id,
            steps=[e['kind'] for e in result.get('events', [])],
            metrics=metrics or {}))
    try:
        stage('打开前端：待处理事件 → 演示与验收 → 处置轨迹。')
        with marker.open('x'):
            marker_created = True
        install_demo_unit(unit, f"""
[Unit]
Description=Autopoiesis controlled self-healing demonstration
[Service]
Type=simple
ExecStart=/bin/sh -c 'if test -f {marker}; then rm -f {marker}; exit 1; fi; exec sleep 1d'
Restart=no
""")
        installed = True
        run_command(['systemctl', 'start', unit], check=False)
        wait_unit_state(unit, 'failed')
        receipt('running', '测试服务已故障，等待连续检测确认')
        def detector():
            return [d for d in failed_units() if d.subject == unit]
        def preflight(action, target, **_):
            return {'eligible': action == 'restart_unit' and target == unit and bool(detector()),
                    'reason': 'exact controlled failed service',
                    'blast_radius': {'scope': 'single-service', 'summary': 'One dedicated test service; the local management gateway is checked independently.', 'targets': [unit]}}
        def execute(action, target, on_command=None, **_):
            nonlocal result, plan_id, command_sink
            command_sink = on_command
            p = svc.prepare(device_id, action, {'device': device_id, 'unit': target}, {},
                actor, '连续两次检测确认专用服务失败，自动恢复并持续检查服务和网关连接。', case_scope)
            plan_id = p['id']
            store.event(plan_id, 'controlled_demo_scope', {'run_id':run_id, 'unit':unit,
                'dataClassification':'controlled_test', 'production_write_switch_unchanged':True})
            receipt('running', '变更前状态与 Diff 已保存，等待演示预览')
            stage(f'Diff 已生成：failed → active；变更 {plan_id}。', 0 if args.test else 15)
            # One use, exact device/target/params and immutable plan digest.
            rule = {'id':run_id, 'device_id':device_id, 'action':action,
                    'target':p['target'], 'params':{}, 'plan_digest':digest(p['plan']),
                    'expires_at':time.time()+180, 'max_uses':1}
            svc.preauthorize(plan_id, [rule])
            receipt('running', '一次性授权已匹配，执行重启并持续观察')
            stage('执行重启，开始持续回读。')
            result = svc.execute(plan_id, actor)
            samples = [e['payload'] for e in result['events'] if e['kind']=='business_readback']
            return {'verdict':{'outcome':'passed' if result['status']=='succeeded' else 'failed',
                'target_recovered':result['status']=='succeeded', 'samples':samples},
                'needs_human':result.get('needs_human',False), 'execution_id': plan_id,
                'incident_id': case_scope, 'change_id': plan_id}
        sentinel = Sentinel(detectors=[detector], preflight=preflight, execute=execute,
                            confirm_polls=2, cooldown_sec=30)
        first = sentinel.poll_once()
        stage('第一次故障检测已记录。', 0 if args.test else 3)
        second = sentinel.poll_once()
        samples = [e['payload'] for e in result.get('events',[]) if e['kind']=='business_readback']
        passed = (len(first.get('detections',[]))==1 and len(second.get('acted',[]))==1
                  and result.get('status')=='succeeded' and len(samples)>=3
                  and all(s.get('target_ok') and s.get('guard_ok') for s in samples[-3:]))
        final = receipt('passed' if passed else 'failed', '服务已恢复并通过持续观察' if passed else '恢复未通过验收',
            {'confirmationPolls':2, 'action':'restart_unit', 'readbackSamples':len(samples),
             'targetRecovered':passed, 'outcome':result.get('status'),
             'changeId':plan_id, 'diffSaved':bool(result.get('plan',{}).get('operations')),
             'preauthorizationActor':result.get('approval',{}).get('actor'),
             'observationSeconds':(len(samples)-1)*(1 if args.test else 5),
             'testMode':args.test})
        if not passed:
            raise RuntimeError('self-healing acceptance failed')
        stage('验收通过；回执与 Diff 已保存。', 0 if args.test else 5)
        print_receipt(final, '/')
        return 0
    except BaseException as error:
        if plan_id:
            p = store.get(plan_id)
            if p['status'] in ('awaiting_approval','approved'):
                store.transition(plan_id,(p['status'],),'cancelled',{'reason':'demo interrupted'})
        receipt('failed', '演示未完成', {'error':str(error)[:400], 'changeId':plan_id})
        raise
    finally:
        if installed:
            remove_demo_unit(unit, unit_path, marker)
            record('controlled_demo_cleanup', {'subject':unit,'demoRunId':run_id,'dataClassification':'controlled_test'})
            stage('已清理本次测试服务；变更、观察回执和演示记录保留。')
        elif marker_created:
            marker.unlink(missing_ok=True)

if __name__ == '__main__':
    raise SystemExit(main())

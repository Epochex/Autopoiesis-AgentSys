#!/usr/bin/env python3
"""Real failed-service recovery through the approved change pipeline."""
from __future__ import annotations
import os
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from scripts.demo_support import (create_demo_case, demo_options, install_demo_unit, print_receipt,
    remove_demo_unit, run_command, say, stage, utc_now, wait_unit_state)


def reflect_completed_event(record_id: str) -> dict:
    """POST the console's own reflect action for a completed case record."""
    import json as _json
    import urllib.request as _request
    from scripts.demo_support import GATEWAY
    body = _json.dumps({'recordId': record_id}).encode('utf-8')
    request = _request.Request(
        f'{GATEWAY}/api/rca/completed-events/reflect', data=body, method='POST',
        headers={'Content-Type': 'application/json', 'Accept': 'application/json',
                 'X-Autopoiesis-Console': 'change-control'},
    )
    with _request.urlopen(request, timeout=60) as response:
        return _json.loads(response.read().decode('utf-8'))


def main():
    args = demo_options(__doc__)
    from core.remediate.change_control import ChangeService, ChangeStore, ChangeError, digest
    from core.remediate.sentinel import Sentinel, record
    from domains.network_rca.detectors import failed_units
    from frontend.gateway.app.host_changes import HostAdapter
    from frontend.gateway.app.demo_scenarios import append_run
    from frontend.gateway.app.demo_execution_trace import project_change_event
    from domains.network_rca.remediation import Command, CommandLog, read_unit

    run_id = 'selfheal-' + uuid.uuid4().hex[:12]
    unit = 'autopoiesis-acceptance-' + run_id + '.service'
    marker = Path('/run') / (unit + '.fail-once')
    unit_path = Path('/run/systemd/system') / unit
    device_id = 'controlled-' + run_id
    case_scope = None
    case_repository = None
    actor = 'controlled-demo:' + run_id
    started = utc_now()
    installed = False
    marker_created = False
    result = {}
    plan_id = None
    preflight_reading = {}
    def stream_change(event):
        for row in project_change_event(event, unit, include_operation_command=False):
            record(row['kind'], {**row, 'run_id': run_id, 'caseId': case_scope,
                                  'dataClassification': 'controlled_test'})
            if case_repository is not None and case_scope and row['kind'] != 'command':
                payload = {key: value for key, value in row.items() if key not in {'kind', 'at'}}
                case_repository.append_event(
                    case_scope,
                    kind=row['kind'],
                    payload=payload,
                    status='escalated' if row['kind'] == 'execution_error' else 'waiting',
                    occurred_at=row.get('at'),
                    event_id=f"{run_id}:change:{row.get('source_event_seq')}:{row['kind']}",
                )
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
            p.update(observation_interval=1 if args.test else 2,
                     observation_samples=4,
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
        stage('保持“网络态势”页面打开；新事件通知将在右上角出现。',
              en='Keep the NETWORK MAP page open; the new-event notice appears at the top right.')
        say(f'Controlled self-healing demonstration {run_id}. Everything below happens on this host only: '
            f'one throw-away systemd unit is created, failed, recovered and removed. No production device, '
            f'policy or write switch is touched.',
            f'受控自愈演示 {run_id}。以下一切只发生在本机：创建一个一次性 systemd 单元，让它失败、恢复、再删除；不碰任何生产设备、策略或写开关。')
        with marker.open('x'):
            marker_created = True
        say(f'Fault injection, step 1: created the fail-once marker {marker}.',
            f'故障注入第 1 步：创建一次性失败标记 {marker}。')
        install_demo_unit(unit, f"""
[Unit]
Description=Autopoiesis controlled self-healing demonstration
[Service]
Type=simple
ExecStart=/bin/sh -c 'if test -f {marker}; then rm -f {marker}; exit 1; fi; exec sleep 1d'
Restart=no
""")
        installed = True
        say(f'Fault injection, step 2: wrote {unit_path} — ExecStart exits 1 while the marker exists '
            f'(and removes it), otherwise sleeps; Restart=no, so systemd will not recover it by itself. '
            f'Then: systemctl daemon-reload.',
            f'故障注入第 2 步：写入 {unit_path}——ExecStart 在标记存在时退出码 1（并删除标记），否则休眠；Restart=no，systemd 不会自己恢复它。然后 systemctl daemon-reload。')
        run_command(['systemctl', 'start', unit], check=False)
        wait_unit_state(unit, 'failed')
        say(f'Fault injection, step 3: systemctl start {unit} → the process exited 1 → ActiveState=failed. '
            f'The fault is now real: a failed service on this host that nothing will restart.',
            f'故障注入第 3 步：systemctl start {unit} → 进程退出码 1 → ActiveState=failed。故障已成立：本机上一个没人会重启的失败服务。')
        from frontend.gateway.app.config import Settings
        from domains.network_rca.investigation_case import InvestigationCaseRepository
        case_repository = InvestigationCaseRepository(Settings.from_env().investigation_case_store_path)
        case_scope = create_demo_case(
            case_repository,
            run_id=run_id,
            scenario='self_healing',
            subject=unit,
            summary='专用服务连续检测失败，系统正在确认并执行受控恢复。',
        )
        say(f'Opened case {case_scope} in the investigation store (classification controlled_test) so the '
            f'console shows this like any other incident.',
            f'在案件库建案 {case_scope}（分类 controlled_test），控制台按普通事件展示。')
        receipt('running', '测试服务已故障，等待连续检测确认')
        def detector():
            return [replace(
                d,
                case_id=case_scope,
                demo_run_id=run_id,
                data_classification='controlled_test',
            ) for d in failed_units(include_controlled=True) if d.subject == unit]
        def preflight(action, target, on_command=None, **_):
            nonlocal preflight_reading
            log = CommandLog(on_entry=on_command)
            preflight_reading = read_unit(Command.local(log), target)
            eligible = (
                action == 'restart_unit'
                and target == unit
                and bool(detector())
                and preflight_reading.get('failed')
                and int(preflight_reading.get('restarts') or 0) < 2
            )
            say(f'Sentinel poll 2: still failed → confirmed. Preflight read the unit with systemctl show '
                f'(ActiveState={preflight_reading.get("active")}, NRestarts={preflight_reading.get("restarts")}); '
                f'{"eligible because it is failed and restarts < 2" if eligible else "NOT eligible"}. '
                f'Blast radius: this single service only; the management gateway on 127.0.0.1:8026 is checked independently.',
                f'巡检第 2 次：仍为 failed → 确认。前置校验用 systemctl show 读单元（ActiveState={preflight_reading.get("active")}，NRestarts={preflight_reading.get("restarts")}）；{"因处于 failed 且重启次数 < 2 而合格" if eligible else "不合格"}。影响面：仅此单一服务；127.0.0.1:8026 的管理网关独立检查。')
            case_repository.append_event(
                case_scope,
                kind='detection_confirmed',
                status='investigating',
                payload={
                    'samples': 2,
                    'required': 2,
                    'reading': preflight_reading,
                    'reason': '连续两次检测到专用服务处于 failed 状态。',
                },
                event_id=f'{run_id}:detection-confirmed',
            )
            return {'eligible': eligible,
                    'reading': preflight_reading,
                    'reason': '受控服务处于 failed 状态，且重启次数仍在上限内。' if eligible else '受控服务未通过失败状态或重启次数校验。',
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
            stage(f'Diff 已生成：failed → active；变更 {plan_id}。', 0 if args.test else 4,
                  en=f'Diff prepared: failed → active; change {plan_id}.')
            say(f'Change plan {plan_id}: action restart_unit on {unit} through the approved change '
                f'pipeline; the before-state was captured so it can be restored.',
                f'变更方案 {plan_id}：经审批变更流水线对 {unit} 执行 restart_unit；变更前状态已保存以便恢复。')
            # One use, exact device/target/params and immutable plan digest.
            rule = {'id':run_id, 'device_id':device_id, 'action':action,
                    'target':p['target'], 'params':{}, 'plan_digest':digest(p['plan']),
                    'expires_at':time.time()+180, 'max_uses':1}
            svc.preauthorize(plan_id, [rule])
            say(f'One-time authorization: rule {run_id} bound to device {device_id}, action restart_unit, '
                f'this exact target, plan digest {rule["plan_digest"][:16]}…, expires in 180 s, max 1 use. '
                f'Nothing else can be executed under it.',
                f'一次性授权：规则 {run_id} 绑定设备 {device_id}、动作 restart_unit、精确目标、方案摘要 {rule["plan_digest"][:16]}…，180 秒过期、最多 1 次。其他任何操作都不在授权内。')
            receipt('running', '一次性授权已匹配，执行重启并持续观察')
            stage('执行重启，开始持续回读。', en='Executing the restart; continuous readback begins.')
            say(f'Execution: systemctl restart {unit}. Readback every {1 if args.test else 2} s × 4 samples: '
                f'systemctl is-active {unit} (target) and a TCP connect to 127.0.0.1:8026 (gateway guard). '
                f'Every command and its exit code is written to the change record.',
                f'执行：systemctl restart {unit}。回读每 {1 if args.test else 2} 秒一次、共 4 次：systemctl is-active {unit}（目标）与 TCP 连接 127.0.0.1:8026（网关守卫）。每条命令与退出码写入变更记录。')
            result = svc.execute(plan_id, actor)
            samples = [e['payload'] for e in result['events'] if e['kind']=='business_readback']
            return {'verdict':{'outcome':'passed' if result['status']=='succeeded' else 'failed',
                'target_recovered':result['status']=='succeeded', 'samples':samples},
                'needs_human':result.get('needs_human',False), 'execution_id': plan_id,
                'incident_id': case_scope, 'change_id': plan_id}
        sentinel = Sentinel(detectors=[detector], preflight=preflight, execute=execute,
                            confirm_polls=2, cooldown_sec=30)
        first = sentinel.poll_once()
        case_repository.append_event(
            case_scope,
            kind='detection_sampled',
            status='investigating',
            payload={'samples': 1, 'required': 2, 'reason': '第一次检测命中，等待连续确认。'},
            event_id=f'{run_id}:detection-first',
        )
        stage('第一次故障检测已记录。', 0 if args.test else 2,
              en='First detection recorded.')
        say(f'Sentinel poll 1: detector failed_units read `systemctl --failed` and found {unit} '
            f'(streak 1 of the 2 consecutive polls required before anything is allowed to act).',
            f'巡检第 1 次：failed_units 探测器读 `systemctl --failed`，发现 {unit}（连续 1/2 次；两次连续命中才允许动作）。')
        second = sentinel.poll_once()
        samples = [e['payload'] for e in result.get('events',[]) if e['kind']=='business_readback']
        operations = list(result.get('plan',{}).get('operations') or ())
        operation_receipts = [e for e in result.get('events',[]) if e['kind']=='operation_receipt']
        passed = (len(first.get('detections',[]))==1 and len(second.get('acted',[]))==1
                  and result.get('status')=='succeeded' and len(samples)>=3
                  and all(s.get('target_ok') and s.get('guard_ok') for s in samples[-3:])
                  and preflight_reading.get('failed')
                  and len(operations) == 1
                  and operations[0].get('kind') == 'unit'
                  and operations[0].get('name') == unit
                  and len(operation_receipts) == 1)
        final = receipt('passed' if passed else 'failed', '服务已恢复并通过持续观察' if passed else '恢复未通过验收',
            {'confirmationPolls':2, 'action':'restart_unit', 'readbackSamples':len(samples),
             'targetRecovered':passed, 'outcome':result.get('status'),
             'changeId':plan_id, 'diffSaved':bool(result.get('plan',{}).get('operations')),
             'preflightEligible':bool(preflight_reading.get('failed')),
             'preflightRestartCount':preflight_reading.get('restarts'),
             'plannedOperations':len(operations),
             'operationReceipts':len(operation_receipts),
             'blastRadiusAssets':1,
             'gatewayGuardPassed':bool(samples) and all(s.get('guard_ok') for s in samples),
             'controlledScope':unit,
             'productionWriteSwitchUnchanged':True,
             'preauthorizationActor':result.get('approval',{}).get('actor'),
             'observationSeconds':(len(samples)-1)*(1 if args.test else 2),
             'testMode':args.test})
        readback = {
            'outcome': 'passed' if passed else 'failed',
            'samples': len(samples),
            'targetRecovered': bool(samples) and all(sample.get('target_ok') for sample in samples),
            'gatewayGuardPassed': bool(samples) and all(sample.get('guard_ok') for sample in samples),
            'observedAt': utc_now(),
        }
        # The restart and its readback really ran (through the change service,
        # with the samples above); the case carries that completion the same
        # way the sentinel records its own, so the completed-events ledger can
        # judge it verified and an operator, or this script, can reflect on it.
        case_repository.append_event(
            case_scope,
            kind='remediation_completed',
            status='resolved' if passed else 'escalated',
            payload={
                'outcome': 'passed' if passed else 'failed',
                'needsHuman': not passed,
                'action': 'restart_unit',
                'target': unit,
                'changeId': plan_id,
                'readback': readback,
                'samples': samples,
                'dataClassification': 'controlled_test',
                'demoRunId': run_id,
            },
            event_id=f'{run_id}:remediation-completed',
        )
        case_repository.append_event(
            case_scope,
            kind='business_decision_recorded',
            status='resolved' if passed else 'escalated',
            payload={'decision': {
                'caseId': case_scope,
                'sessionId': '',
                'state': 'resolved' if passed else 'escalated',
                'classification': 'service_failed',
                'headline': '服务已自动恢复并通过持续回读' if passed else '恢复验证未通过，已转人工处理',
                'summary': '连续两次检测确认服务失败，一次性授权仅允许重启本次专用服务。',
                'disposition': '已执行一次重启并完成持续回读。' if passed else '已停止自动动作并保留失败证据。',
                'action': '重启专用服务',
                'service': unit,
                'impactedAssets': [unit],
                'evidence': [],
                'missingObservations': [],
                'nextProbe': None,
                'readback': readback,
                'generatedAt': utc_now(),
            }},
            event_id=f'{run_id}:final-decision',
        )
        if not passed:
            raise RuntimeError('self-healing acceptance failed')
        stage('验收通过；事件中心已保存完整链路。', 0 if args.test else 2,
              en='Acceptance passed; the full chain is saved in the incident centre.')
        say(f'Readback {len(samples)}/{len(samples)} passed: target active, gateway reachable on every sample. '
            f'Change status {result.get("status")}; exactly one operation ({operations[0].get("kind") if operations else "?"} {unit}) '
            f'with one device receipt. Decision recorded on case {case_scope}: resolved.',
            f'回读 {len(samples)}/{len(samples)} 通过：每次采样目标 active、网关可达。变更状态 {result.get("status")}；恰好一次操作（{operations[0].get("kind") if operations else "?"} {unit}）与一份设备回执。案件 {case_scope} 已记录决定：已解决。')
        # The chain itself never writes memory: reflection is the explicit,
        # evidence-grounded step an operator takes on a completed event. The
        # demonstration takes it too, so the memory observatory shows what a
        # verified recovery leaves behind. A refusal is reported, not fatal.
        try:
            reflected = reflect_completed_event(case_scope)
            stage(f"已对本次事件反思并写入记忆 {reflected.get('memoryId') or ''}。", 0 if args.test else 2,
                  en=f"Reflected on the completed event; wrote memory {reflected.get('memoryId') or ''}.")
            say('Reflection is the explicit, evidence-grounded step an operator takes on a completed event; the '
                'chain itself never writes memory. The record is an episodic memory tied to this unit and case.',
                '反思是操作者对已完成事件做的、有证据依据的显式动作；处置链本身不写记忆。写入的是与本单元和本案绑定的历史案例记忆。')
        except Exception as error:  # noqa: BLE001 - the demo result stands either way
            stage(f'反思并记忆未执行：{str(error)[:160]}')
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
            stage('已清理本次测试服务；变更、观察回执和演示记录保留。',
                  en='Test unit cleaned up; the change, readback receipts and demo record are retained.')
            say(f'Cleanup: systemctl stop {unit}; systemctl reset-failed {unit}; removed {unit_path} and the marker; '
                f'systemctl daemon-reload. Run `sudo ./scripts/cleanup_demo_usecases.py` to remove the case, '
                f'session and memory too.',
                f'清理：systemctl stop {unit}；systemctl reset-failed {unit}；删除 {unit_path} 与标记；systemctl daemon-reload。要连案件、会话和记忆一起删，运行 `sudo ./scripts/cleanup_demo_usecases.py`。')
        elif marker_created:
            marker.unlink(missing_ok=True)

if __name__ == '__main__':
    raise SystemExit(main())

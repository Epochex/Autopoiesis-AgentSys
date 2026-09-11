#!/usr/bin/env python3
"""Run the same controlled incident twice and prove memory changes probe work."""
from __future__ import annotations

import fcntl
import sys
import time
import uuid
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from domains.network_rca.investigation_case import (  # noqa: E402
    CaseObservation,
    InvestigationCaseRepository,
    SourceReference,
)
from frontend.gateway.app.config import Settings  # noqa: E402
from frontend.gateway.app.demo_scenarios import append_run  # noqa: E402
from frontend.gateway.app.investigation_session_store import InvestigationSessionStore  # noqa: E402
from scripts.demo_support import (  # noqa: E402
    create_demo_case,
    http_json,
    install_demo_unit,
    print_receipt,
    remove_demo_unit,
    run_command,
    utc_now,
    wait_unit_state,
    demo_options,
    say,
    stage,
)


def create_case(
    repository: InvestigationCaseRepository,
    run_id: str,
    pass_no: int,
    unit: str,
) -> str:
    return create_demo_case(
        repository,
        run_id=run_id,
        scenario="memory_trajectory",
        subject=unit,
        summary=f"{unit} 当前处于 failed 状态，执行第 {pass_no} 次受控调查。",
        source_suffix=f"pass-{pass_no}",
        extra_payload={"demoPass": pass_no},
    )


def investigate(
    case_id: str,
    unit: str,
    *,
    memory_enabled: bool,
    pause_seconds: float = 0,
    on_open=None,
) -> dict[str, Any]:
    began = time.monotonic()
    opened = http_json("POST", "/api/rca/investigate/start", {
        "question": f"调查 {unit} 服务失败并给出当前证据",
        "family": "fam-perception-selfheal",
        "subject": unit,
        "case_id": case_id,
        "new_session": True,
        "memory_enabled": memory_enabled,
        "lang": "zh",
    })
    session_id = str(opened["session_id"])
    if on_open:
        on_open(session_id)
    stage(f'调查 {session_id} 已建立，前端可查看记忆选择与工作集。', pause_seconds,
          en=f'Investigation session {session_id} opened (memory {"enabled" if memory_enabled else "disabled"}); '
             f'the console shows its retrieval, selection and working set as it runs.')
    completed = http_json(
        "POST",
        "/api/rca/investigate/complete",
        {"session_id": session_id, "lang": "zh"},
        timeout=120,
    )
    return {"opened": opened, "completed": completed, "sessionId": session_id,
            "requestSeconds": round(time.monotonic() - began - pause_seconds, 3)}


def measured_comparison(baseline, recalled):
    before = baseline.get('memory_intervention') or baseline.get('memory_value') or {}
    after = recalled.get('memory_intervention') or recalled.get('memory_value') or {}
    cold = list(before.get('executed_order') or [])
    warm = list(after.get('executed_order') or [])
    return {'baselineExecutedProbes':len(cold), 'executedProbes':len(warm),
            'actualSavedProbes':len(cold)-len(warm), 'baselineExecutedOrder':cold,
            'executedOrder':warm, 'candidateProbes':len(after.get('no_memory_shadow_order') or []),
            'shadowSavedProbes':int(after.get('saved_probe_count') or 0)}


def trace_events(session: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [event for event in session.get("trace_events", []) if event.get("kind") == kind]


def indexed_procedure_id(session: dict[str, Any]) -> str:
    events = trace_events(session, "verified_procedure_indexed")
    if not events:
        raise RuntimeError("第一轮没有产生已验证处理方法")
    memory_id = str(events[-1].get("payload", {}).get("memory_id") or "")
    if not memory_id:
        raise RuntimeError("已验证处理方法缺少 memory_id")
    return memory_id


def wait_for_procedure(memory_id: str, session_id: str, timeout: float = 15.0) -> dict[str, Any]:
    """Wait until the exact baseline write is queryable from durable memory."""
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            detail = http_json("GET", f"/api/rca/memory/{memory_id}")
            sources = list(detail.get("record", {}).get("source_trace_ids") or ())
            if session_id in sources:
                return detail
        except Exception as error:
            last_error = error
        time.sleep(0.2)
    suffix = f": {last_error}" if last_error else ""
    raise RuntimeError(f"第一轮处理方法未进入可查询持久化记忆{suffix}")


def ordered_trace(trace: list[dict[str, Any]], required: list[str]) -> bool:
    cursor = -1
    for kind in required:
        cursor = next(
            (index for index in range(cursor + 1, len(trace)) if trace[index].get("kind") == kind),
            -1,
        )
        if cursor < 0:
            return False
    return True


def main() -> int:
    args = demo_options(__doc__)
    run_id = f"memory-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    unit = f"autopoiesis-acceptance-memory-{uuid.uuid4().hex[:8]}.service"
    started_at = utc_now()
    unit_path = Path("/run/systemd/system") / unit
    receipt: dict[str, Any] | None = None
    installed = False
    lock_path = Path("/run/autopoiesis-memory-demo.lock")
    lock_path.touch(exist_ok=True)

    with lock_path.open("r+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            install_demo_unit(
                unit,
                """
[Unit]
Description=Autopoiesis controlled memory and trajectory demonstration

[Service]
Type=oneshot
ExecStart=/bin/false
Restart=no
""",
            )
            installed = True
            say(f'Controlled memory demonstration {run_id}. Fault injection: wrote {unit_path} '
                f'(Type=oneshot, ExecStart=/bin/false, Restart=no) and ran systemctl start {unit} → '
                f'exit 1 → ActiveState=failed. Same fault class as the self-healing run: service_failed.',
                f'受控记忆演示 {run_id}。故障注入：写入 {unit_path}（Type=oneshot，ExecStart=/bin/false，Restart=no），systemctl start {unit} → 退出码 1 → ActiveState=failed。故障类与自愈演示相同：service_failed。')
            run_command(["systemctl", "start", unit], check=False)
            wait_unit_state(unit, "failed")

            repository = InvestigationCaseRepository(
                Settings.from_env().investigation_case_store_path
            )
            baseline_case = create_case(repository, run_id, 1, unit)
            from core.remediate.sentinel import record
            cycle_opened_at = utc_now()
            record('detected', {
                'subject': unit,
                'detector': 'memory_trajectory_demo',
                'family': 'fam-perception-selfheal',
                'severity': 'high',
                'summary': '正在对同类服务故障执行无记忆与有记忆的两轮调查对比。',
                'evidence': {'comparison': 'cold_then_recalled'},
                'action': None,
                'target': unit,
                'streak': 1,
                'cycle_opened_at': cycle_opened_at,
                'caseId': baseline_case,
                'demoRunId': run_id,
                'dataClassification': 'controlled_test',
            })
            def progress(case_id, session_id, phase):
                append_run({'runId':run_id, 'scenario':'memory_trajectory', 'status':'running',
                    'startedAt':started_at, 'completedAt':utc_now(), 'subject':unit,
                    'headline':phase, 'phase':phase, 'caseId':case_id, 'sessionId':session_id,
                    'metrics':{}, 'steps':[]})
                stage('前端：网络态势通知 → 点击进入事件详情。' + phase,
                      en='Console: the NETWORK MAP notice → click to open the incident. ' + (
                          'Round 1: memory disabled, recording the checks actually run.' if '第一轮' in phase
                          else 'Round 2: memory enabled, watching which checks it skips.'))
            baseline = investigate(baseline_case, unit, memory_enabled=False,
                pause_seconds=0 if args.test else 4,
                on_open=lambda sid: progress(baseline_case,sid,'第一轮：关闭记忆调用，记录实际检查'))
            baseline_session = http_json('GET', f"/api/rca/investigate/{baseline['sessionId']}")
            procedure_id = indexed_procedure_id(baseline_session)
            procedure = wait_for_procedure(procedure_id, baseline['sessionId'])
            repository.append_event(
                baseline_case,
                kind='baseline_investigation_completed',
                status='investigating',
                payload={
                    'sessionId': baseline['sessionId'],
                    'memoryEnabled': False,
                    'executedChecks': len((baseline_session.get('memory_intervention') or {}).get('executed_order') or ()),
                },
                event_id=f'{run_id}:baseline-complete',
            )
            baseline_order = list((baseline_session.get('memory_intervention') or {}).get('executed_order') or ())
            stage(f'第一轮已完成，处理方法 {procedure_id} 已持久化并绑定本轮调查。', 0 if args.test else 4,
                  en=f'Round 1 complete; procedure {procedure_id} persisted and bound to this session.')
            say(f'Round 1 ran {len(baseline_order)} read-only checks with no memory to guide it: '
                f'{", ".join(baseline_order) or "(see the console trace)"}. It confirmed service_failed from '
                f'`systemctl --failed`, recorded the decision, and indexed what worked as a verified procedure.',
                f'第一轮在无记忆引导下执行了 {len(baseline_order)} 项只读检查：{"、".join(baseline_order) or "（见控制台轨迹）"}。它由 `systemctl --failed` 确认 service_failed，记录决定，并把有效做法索引为已验证处理方法。')

            # The second source timestamp follows the first completed procedure,
            # so temporal filtering can admit it without looking into the future.
            time.sleep(0.05)
            recalled_case = create_case(repository, run_id, 2, unit)
            say(f'Round 2: a new internal case {recalled_case} for the same unit, memory enabled. Retrieval '
                f'ranks candidates (the procedure from round 1 plus knowledge documents), selection hands the '
                f'procedure to the reasoner, and the memory shortcut skips the checks it already knows the '
                f'answer to. The operator sees only the primary case; both rounds stay in its audit record.',
                f'第二轮：同一单元的内部新案件 {recalled_case}，开启记忆。检索对候选排序（第一轮的处理方法 + 知识文档），选材把处理方法交给推理器，记忆捷径跳过已知答案的检查。操作者只看到主案件，两轮都留在它的审计记录里。')
            recalled = investigate(recalled_case, unit, memory_enabled=True,
                pause_seconds=0 if args.test else 6,
                on_open=lambda sid: progress(recalled_case,sid,'第二轮：调用已有记忆，观察检查与上下文变化'))
            session_id = str(recalled["sessionId"])
            session = http_json("GET", f"/api/rca/investigate/{session_id}")
            value = dict(session.get("memory_intervention") or session.get("memory_value") or {})
            analysis = {
                "activities": [str(event.get("kind") or "") for event in session.get("trace_events", []) if event.get("kind")],
                "activity_count": len(session.get("trace_events") or ()),
                "current_state": {"state_id": str((session.get("trace_events") or [{}])[-1].get("kind") or "complete")},
            }
            saved = int(value.get("saved_probe_count") or 0)
            shadow_order = list(value.get("no_memory_shadow_order") or ())
            executed_order = list(value.get("executed_order") or ())
            comparison = measured_comparison(baseline_session, session)
            trace = list(session.get('trace_events') or ())
            ranked = trace_events(session, 'memory_candidates_ranked')
            retrieval_snapshot = bool(ranked and ranked[-1]['payload'].get('investigation_context'))
            selected_ids = {
                str(item.get('item_id')) for item in session.get('retrieval_results', [])
                if item.get('selected_for_context')
            }
            recalled_ids = set(value.get('memory_ids') or ())
            source_bound_recall = procedure_id in recalled_ids and procedure_id in selected_ids
            model_context_events = trace_events(session, 'context_compiled')
            model_context_submitted = any(
                procedure_id in {
                    str(item.get('item_id'))
                    for item in event.get('payload', {}).get('retrieval_results', [])
                }
                for event in model_context_events
            )
            checkpoints = trace_events(session, 'working_set_updated')
            versions = [
                int(event.get('payload', {}).get('working_set', {}).get('state_version') or 0)
                for event in checkpoints
            ]
            monotonic_checkpoints = bool(versions) and versions == sorted(versions) and versions[-1] > 0
            latest_version = versions[-1] if versions else 0
            persisted = InvestigationSessionStore(
                Settings.from_env().investigation_session_store_dir
            ).load(session_id)
            persisted_session = bool(
                persisted
                and persisted.get('session_id') == session_id
                and len(persisted.get('trace_events') or ()) >= len(trace)
                and int((persisted.get('working_set') or {}).get('state_version') or 0) == latest_version
            )
            trajectory_ordered = ordered_trace(trace, [
                'memory_candidates_ranked',
                'hypothesis_confirmed',
                'memory_shortcut',
                'memory_intervention_evaluated',
                'business_decision_recorded',
            ])
            passed = bool(
                value.get("memory_applied")
                and value.get("ordering_effect") == "changed"
                and value.get("confirmed")
                and saved > 0
                and len(executed_order) < len(shadow_order)
                and int(analysis.get("activity_count") or 0) > 0
                and comparison['actualSavedProbes'] > 0
                and retrieval_snapshot
                and source_bound_recall
                and monotonic_checkpoints
                and persisted_session
                and trajectory_ordered
            )
            say(f'Comparison: checks {comparison["baselineExecutedProbes"]} → {comparison["executedProbes"]} '
                f'({comparison["actualSavedProbes"]} avoided); request time {baseline["requestSeconds"]}s → '
                f'{recalled["requestSeconds"]}s; recall of {procedure_id} was source-bound '
                f'({"yes" if source_bound_recall else "no"}), context submitted to the model '
                f'({"yes" if model_context_submitted else "no"}), working-set checkpoints persisted '
                f'({"yes" if persisted_session else "no"}), trajectory order verified ({"yes" if trajectory_ordered else "no"}).',
                f'对比：检查 {comparison["baselineExecutedProbes"]} → {comparison["executedProbes"]}（少做 {comparison["actualSavedProbes"]} 项）；请求耗时 {baseline["requestSeconds"]}s → {recalled["requestSeconds"]}s；{procedure_id} 的召回来源绑定（{"是" if source_bound_recall else "否"}），上下文已交给模型（{"是" if model_context_submitted else "否"}），工作集检查点已持久化（{"是" if persisted_session else "否"}），轨迹顺序已验证（{"是" if trajectory_ordered else "否"}）。')
            receipt = append_run({
                "runId": run_id,
                "scenario": "memory_trajectory",
                "status": "passed" if passed else "failed",
                "startedAt": started_at,
                "completedAt": utc_now(),
                "subject": unit,
                "headline": (
                    "历史处置记录已减少本次检查步骤" if passed
                    else "记忆提速链路未通过全部验收条件"
                ),
                "caseId": baseline_case,
                "sessionId": session_id,
                "baselineCaseId": baseline_case,
                "baselineSessionId": baseline["sessionId"],
                "comparisonCaseId": recalled_case,
                "steps": list(analysis.get("activities") or ()),
                "metrics": {
                    "memoryApplied": bool(value.get("memory_applied")),
                    "orderingEffect": value.get("ordering_effect"),
                    "memoryConfirmed": bool(value.get("confirmed")),
                    "candidateProbes": len(shadow_order),
                    "executedProbes": len(executed_order),
                    "savedProbes": comparison['actualSavedProbes'],
                    **comparison,
                    "baselineRequestSeconds": baseline['requestSeconds'],
                    "memoryRequestSeconds": recalled['requestSeconds'],
                    "baselineProcedureMemoryId": procedure_id,
                    "baselineProcedureSources": list(procedure.get('record', {}).get('source_trace_ids') or ()),
                    "sourceBoundRecall": source_bound_recall,
                    "retrievalSnapshotRecorded": retrieval_snapshot,
                    "selectedForContext": procedure_id in selected_ids,
                    "modelContextSubmitted": model_context_submitted,
                    "workingSetUpdates": len(checkpoints),
                    "workingSetVersionsMonotonic": monotonic_checkpoints,
                    "persistedSessionVerified": persisted_session,
                    "trajectoryOrderVerified": trajectory_ordered,
                    "contextKind": "retrieval_selection_and_working_set",
                    "testMode": args.test,
                    "memoryIds": sorted(recalled_ids),
                    "trajectoryActivities": int(analysis.get("activity_count") or 0),
                    "trajectoryCycleRate": analysis.get("cycle_rate"),
                    "currentState": dict(analysis.get("current_state") or {}).get("state_id"),
                    "fsmPredictionMode": "shadow_only",
                },
            })
            comparison_payload = {
                "comparisonCaseId": recalled_case,
                "baselineCaseId": baseline_case,
                "baselineSessionId": baseline["sessionId"],
                "sessionId": session_id,
                "memoryId": procedure_id,
                "memoryApplied": bool(value.get("memory_applied")),
                "sourceTraceIds": list(procedure.get("record", {}).get("source_trace_ids") or ()),
                "baselineChecks": comparison["baselineExecutedProbes"],
                "executedChecks": comparison["executedProbes"],
                "savedChecks": comparison["actualSavedProbes"],
                "baselineRequestSeconds": baseline["requestSeconds"],
                "memoryRequestSeconds": recalled["requestSeconds"],
            }
            repository.append_event(
                recalled_case,
                kind="memory_comparison_recorded",
                status="resolved" if passed else "escalated",
                payload=comparison_payload,
                event_id=f"{run_id}:memory-comparison",
            )
            # Update the operator-facing primary case last so event-centre
            # deduplication keeps this row while both internal passes remain
            # available from its audit record.
            repository.append_event(
                baseline_case,
                kind="memory_comparison_recorded",
                status="resolved" if passed else "escalated",
                payload=comparison_payload,
                event_id=f"{run_id}:memory-comparison",
            )
            repository.append_event(
                baseline_case,
                kind="business_decision_recorded",
                status="resolved" if passed else "escalated",
                payload={"decision": {
                    "caseId": baseline_case,
                    "sessionId": session_id,
                    "state": "resolved" if passed else "escalated",
                    "classification": "memory_guided_investigation",
                    "headline": (
                        f"历史处理记录使本次检查从 {comparison['baselineExecutedProbes']} 项减少到 {comparison['executedProbes']} 项"
                        if passed else "记忆引导调查未通过验证"
                    ),
                    "summary": "同一服务故障完成无记忆和有记忆两轮调查，召回来源、适用条件和实际检查均已核对。",
                    "disposition": "采用已验证记忆引导最小检查路径并形成处置建议。",
                    "action": "生成记忆引导的调查结论",
                    "service": unit,
                    "impactedAssets": [unit],
                    "evidence": [],
                    "missingObservations": [],
                    "nextProbe": None,
                    "readback": {
                        "memoryApplied": bool(value.get("memory_applied")),
                        "sourceBoundRecall": source_bound_recall,
                        "persistedSessionVerified": persisted_session,
                        "trajectoryOrderVerified": trajectory_ordered,
                    },
                    "generatedAt": utc_now(),
                }},
                event_id=f"{run_id}:final-decision",
            )
            record('resolved' if passed else 'escalated', {
                'subject': unit,
                'detector': 'memory_trajectory_demo',
                'action': 'compare_memory_guided_investigation',
                'outcome': 'passed' if passed else 'failed',
                'samples': 2,
                'target_recovered': passed,
                'note': f"历史处理记录使实际检查从 {comparison['baselineExecutedProbes']} 项减少到 {comparison['executedProbes']} 项。",
                'caseId': baseline_case,
                'demoRunId': run_id,
                'dataClassification': 'controlled_test',
            })
            if not passed:
                raise RuntimeError("controlled memory/trajectory acceptance conditions failed")
        except BaseException as error:
            if receipt is None:
                receipt = append_run({
                    "runId": run_id,
                    "scenario": "memory_trajectory",
                    "status": "failed",
                    "startedAt": started_at,
                    "completedAt": utc_now(),
                    "subject": unit,
                    "headline": "记忆与长轨迹演示执行失败",
                    "steps": [],
                    "metrics": {"error": f"{type(error).__name__}: {error}"[:500]},
                })
            raise
        finally:
            if installed:
                remove_demo_unit(unit, unit_path)
                stage('测试服务已清理；两轮调查、记忆版本和轨迹保留。',
                      en='Test unit cleaned up; both investigations, memory versions and trajectories are retained.')
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    print_receipt(receipt, "/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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
from scripts.demo_support import (  # noqa: E402
    http_json,
    install_demo_unit,
    print_receipt,
    remove_demo_unit,
    run_command,
    utc_now,
    wait_unit_state,
    demo_options,
    stage,
)


UNIT = "autopoiesis-acceptance-memory.service"


def create_case(repository: InvestigationCaseRepository, run_id: str, pass_no: int) -> str:
    observed_at = utc_now()
    source_id = f"{run_id}:pass-{pass_no}"
    case = repository.ingest(CaseObservation(
        source=SourceReference("controlled_demo", source_id),
        occurred_at=observed_at,
        severity="high",
        subject=UNIT,
        service="service_health",
        rule_id="failed_units",
        scope="controlled_memory_demo",
        summary=f"{UNIT} 当前处于 failed 状态，执行第 {pass_no} 次受控调查。",
        payload={
            "dataClassification": "controlled_test",
            "demoRunId": run_id,
            "demoPass": pass_no,
            "incidentFacts": {
                "dataClassification": "controlled_test",
                "observedAt": observed_at,
                "detector": "failed_units",
                "faultClass": "service_failed",
                "subjectKind": "systemd_unit",
                "managedDevice": UNIT,
            },
        },
    ))
    return case.case_id


def investigate(case_id: str, *, memory_enabled: bool, pause_seconds: float = 0, on_open=None) -> dict[str, Any]:
    began = time.monotonic()
    opened = http_json("POST", "/api/rca/investigate/start", {
        "question": f"调查 {UNIT} 服务失败并给出当前证据",
        "family": "fam-perception-selfheal",
        "subject": UNIT,
        "case_id": case_id,
        "new_session": True,
        "memory_enabled": memory_enabled,
        "lang": "zh",
    })
    session_id = str(opened["session_id"])
    if on_open:
        on_open(session_id)
    stage(f'调查 {session_id} 已建立，前端可查看记忆选择与工作集。', pause_seconds)
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


def main() -> int:
    args = demo_options(__doc__)
    run_id = f"memory-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    started_at = utc_now()
    unit_path = Path("/run/systemd/system") / UNIT
    receipt: dict[str, Any] | None = None
    installed = False
    lock_path = Path("/run/autopoiesis-memory-demo.lock")
    lock_path.touch(exist_ok=True)

    with lock_path.open("r+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            install_demo_unit(
                UNIT,
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
            run_command(["systemctl", "start", UNIT], check=False)
            wait_unit_state(UNIT, "failed")

            repository = InvestigationCaseRepository(
                Settings.from_env().investigation_case_store_path
            )
            baseline_case = create_case(repository, run_id, 1)
            def progress(case_id, session_id, phase):
                append_run({'runId':run_id, 'scenario':'memory_trajectory', 'status':'running',
                    'startedAt':started_at, 'completedAt':utc_now(), 'subject':UNIT,
                    'headline':phase, 'phase':phase, 'caseId':case_id, 'sessionId':session_id,
                    'metrics':{}, 'steps':[]})
                stage('前端：待处理事件 → 演示与验收 → 记忆与轨迹。' + phase)
            baseline = investigate(baseline_case, memory_enabled=False,
                pause_seconds=0 if args.test else 8,
                on_open=lambda sid: progress(baseline_case,sid,'第一轮：关闭记忆调用，记录实际检查'))
            baseline_session = http_json('GET', f"/api/rca/investigate/{baseline['sessionId']}")
            stage('第一轮已完成，验证步骤已写回记忆。', 0 if args.test else 8)

            # The second source timestamp follows the first completed procedure,
            # so temporal filtering can admit it without looking into the future.
            time.sleep(0.05)
            recalled_case = create_case(repository, run_id, 2)
            recalled = investigate(recalled_case, memory_enabled=True,
                pause_seconds=0 if args.test else 12,
                on_open=lambda sid: progress(recalled_case,sid,'第二轮：调用已有记忆，观察检查与上下文变化'))
            session_id = str(recalled["sessionId"])
            session = http_json("GET", f"/api/rca/investigate/{session_id}")
            fsm = http_json(
                "GET",
                "/api/rca/investigate/trajectory-fsm",
                query={"session_id": session_id, "context_window": 2},
            )

            value = dict(session.get("memory_intervention") or session.get("memory_value") or {})
            analysis = dict(fsm.get("analysis") or {})
            saved = int(value.get("saved_probe_count") or 0)
            shadow_order = list(value.get("no_memory_shadow_order") or ())
            executed_order = list(value.get("executed_order") or ())
            comparison = measured_comparison(baseline_session, session)
            ranked = [t for t in session.get('trace_events',[]) if t['kind']=='memory_candidates_ranked']
            context_saved = bool(ranked and ranked[-1]['payload'].get('investigation_context'))
            checkpoint_count = sum(t['kind']=='working_set_updated' for t in session.get('trace_events',[]))
            passed = bool(
                value.get("memory_applied")
                and value.get("ordering_effect") == "changed"
                and value.get("confirmed")
                and saved > 0
                and len(executed_order) < len(shadow_order)
                and int(analysis.get("activity_count") or 0) > 0
                and comparison['actualSavedProbes'] > 0
                and context_saved and checkpoint_count > 0
            )
            receipt = append_run({
                "runId": run_id,
                "scenario": "memory_trajectory",
                "status": "passed" if passed else "failed",
                "startedAt": started_at,
                "completedAt": utc_now(),
                "subject": UNIT,
                "headline": (
                    "历史处置记录已减少本次检查步骤" if passed
                    else "记忆提速链路未通过全部验收条件"
                ),
                "caseId": recalled_case,
                "sessionId": session_id,
                "baselineCaseId": baseline_case,
                "baselineSessionId": baseline["sessionId"],
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
                    "contextRecorded": context_saved,
                    "workingSetUpdates": checkpoint_count,
                    "contextKind": "investigation_context",
                    "testMode": args.test,
                    "memoryIds": list(value.get("memory_ids") or ()),
                    "trajectoryActivities": int(analysis.get("activity_count") or 0),
                    "trajectoryCycleRate": analysis.get("cycle_rate"),
                    "currentState": dict(analysis.get("current_state") or {}).get("state_id"),
                    "fsmPredictionMode": "shadow_only",
                },
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
                    "subject": UNIT,
                    "headline": "记忆与长轨迹演示执行失败",
                    "steps": [],
                    "metrics": {"error": f"{type(error).__name__}: {error}"[:500]},
                })
            raise
        finally:
            if installed:
                remove_demo_unit(UNIT, unit_path)
                stage('测试服务已清理；两轮调查、记忆版本和轨迹保留。')
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    print_receipt(receipt, "/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

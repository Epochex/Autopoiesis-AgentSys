#!/usr/bin/env python3
"""Execute the six incident business-value checks against real host state.

The runner creates loopback-only transient systemd targets, lands their observed
state as durable investigation cases, drives the normal investigation and
remediation code, and removes the transient units in ``finally``.  It uses the
Codex CLI only for the open-root proposal; all confirmation comes from later
read-only commands executed by the product code.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
RUN_ROOT = Path("/data/autopoiesis-test-artifacts/business-value-acceptance") / RUN_ID
RUN_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["AUTOPOIESIS_REMEDIATION_BUDGET"] = str(RUN_ROOT / "remediation-budget.json")
os.environ["AUTOPOIESIS_REMEDIATION_LOG"] = str(RUN_ROOT / "remediation-runs.jsonl")
os.environ["AUTOPOIESIS_REMEDIATION_STOP"] = str(RUN_ROOT / "remediation-stop.json")
os.environ["AUTOPOIESIS_INVESTIGATION_PAIR_LOG"] = str(RUN_ROOT / "investigation-pairs.jsonl")
# The acceptance run creates two separate, observed incidents on the same
# loopback target within minutes.  Production keeps a long recurrence cooldown;
# this isolated ledger removes only that waiting period while preserving the
# per-incident, per-asset and failure-domain budgets.
os.environ["AUTOPOIESIS_REMEDIATION_COOLDOWN"] = "0"
os.environ["AUTOPOIESIS_REMEDIATION_BACKOFF_BASE"] = "0"
os.environ["AUTOPOIESIS_PAIR_REPETITIONS"] = "3"

from core.eval.business_value_acceptance import evaluate_business_value
from core.memory.store import TieredMemoryStore
from core.remediate import BakeIn
from domains.network_rca.investigation_case import CaseObservation, SourceReference
from frontend.gateway.app import investigate, investigation_cases, main, remediation


TARGET = REPO_ROOT / "scripts" / "business_value_fault_target.py"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _command(argv: list[str], *, check: bool = True, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )


def _unit_state(unit: str) -> str:
    done = _command(["systemctl", "show", unit, "-p", "ActiveState", "--value"], check=False)
    return done.stdout.strip()


def _wait_state(unit: str, wanted: set[str], *, timeout: float = 15.0) -> str:
    deadline = time.monotonic() + timeout
    latest = ""
    while time.monotonic() < deadline:
        latest = _unit_state(unit)
        if latest in wanted:
            return latest
        time.sleep(0.2)
    raise RuntimeError(f"{unit} did not enter {sorted(wanted)}; latest={latest!r}")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


def _start_unit(unit: str, *, mode: str, port: int, state_file: Path | None = None) -> None:
    argv = [
        "systemd-run",
        f"--unit={unit}",
        "--property=Type=simple",
        "--property=Restart=no",
        sys.executable,
        str(TARGET),
        "--mode",
        mode,
        "--port",
        str(port),
    ]
    if state_file is not None:
        argv.extend(["--state-file", str(state_file)])
    _command(argv)
    _wait_state(unit, {"active"} if mode == "schema-mismatch" else {"failed"})


def _stop_unit(unit: str) -> None:
    _command(["systemctl", "stop", unit], check=False)
    _command(["systemctl", "reset-failed", unit], check=False)


def _inject_recurrence_failure(unit: str, port: int) -> None:
    _command([
        "curl", "-sS", "--max-time", "5",
        f"http://127.0.0.1:{port}/inject-failure",
    ])
    _wait_state(unit, {"failed"})


def _case(
    *,
    source_id: str,
    unit: str,
    summary: str,
    facts: Mapping[str, Any] | None = None,
) -> Any:
    observed_at = _now()
    payload_facts = {
        "dataClassification": "observed",
        "observedAt": observed_at,
        "detector": "controlled_loopback_acceptance",
        **dict(facts or {}),
    }
    return main._case_repository().ingest(CaseObservation(
        source=SourceReference("controlled_fault", source_id),
        occurred_at=observed_at,
        severity="high",
        subject=unit,
        service="endpoint",
        rule_id="availability_guard_v1",
        scope="controlled_loopback",
        summary=summary,
        payload={
            "dataClassification": "observed",
            "incidentFacts": payload_facts,
            "acceptanceRunId": RUN_ID,
        },
    ))


class CodexCLIClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        schema_name: str,
    ) -> dict[str, Any]:
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "diagnosis", "root_cause", "root_hypothesis_id", "citations",
                "need_commands", "verification", "runbook",
            ],
            "properties": {
                "diagnosis": {"type": "string"},
                "root_cause": {"type": "string"},
                "root_hypothesis_id": {"type": "string"},
                "citations": {"type": "array", "items": {"type": "string"}},
                "need_commands": {"type": "array", "items": {"type": "string"}},
                "verification": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["command", "operator", "value", "case_sensitive"],
                        "properties": {
                            "command": {"type": "string"},
                            "operator": {
                                "type": "string",
                                "enum": [
                                    "contains", "not_contains", "regex", "not_regex",
                                    "equals", "not_equals",
                                ],
                            },
                            "value": {"type": "string"},
                            "case_sensitive": {"type": "boolean"},
                        },
                    },
                },
                "runbook": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["n", "risk", "what", "command", "why"],
                        "properties": {
                            "n": {"type": "integer"},
                            "risk": {"type": "string", "enum": ["readonly", "auto", "gated"]},
                            "what": {"type": "string"},
                            "command": {"type": "string"},
                            "why": {"type": "string"},
                        },
                    },
                },
            },
        }
        prompt = (
            "Act only as the JSON analysis backend described by these messages. "
            "Do not inspect files and do not run tools. The case evidence contains a "
            "loopback health URL and a systemd unit. For a new root, declare at least "
            "two safe read-only checks from different diagnostic signal families, with "
            "predicates that can be evaluated literally. Use only one argv-only command "
            "per check. Accepted forms include `systemctl show UNIT -p PROPERTY --value`, "
            "`systemctl status UNIT --no-pager`, `journalctl -u UNIT -n 40 --no-pager`, "
            "and `curl -s -m 5 PRIVATE_URL`. Do not combine flags and do not use shell "
            "variables, pipes, redirection, substitution, or command chaining. Return "
            "the schema object only.\n\n"
            + json.dumps(messages, ensure_ascii=False)
        )
        with tempfile.TemporaryDirectory(prefix="autopoiesis-codex-eval-") as temporary:
            root = Path(temporary)
            schema_path = root / "schema.json"
            output_path = root / "answer.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            done = _command(
                [
                    "codex", "exec", "--ephemeral", "--ignore-rules", "--sandbox", "read-only",
                    "-C", str(REPO_ROOT), "--output-schema", str(schema_path),
                    "-o", str(output_path), prompt,
                ],
                check=False,
                timeout=240.0,
            )
            if done.returncode != 0 or not output_path.exists():
                raise RuntimeError(
                    f"Codex analysis failed rc={done.returncode}: "
                    f"{(done.stderr or done.stdout)[-800:]}"
                )
            raw = output_path.read_text(encoding="utf-8").strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
            payload = json.loads(raw)
        self.calls.append({
            "schema": schema_name,
            "root_cause": payload.get("root_cause"),
            "root_hypothesis_id": payload.get("root_hypothesis_id"),
            "verification_commands": [
                item.get("command") for item in payload.get("verification") or ()
            ],
        })
        return payload


def _refresh_ipv6_takeover() -> str | None:
    for case in main._case_repository().list(limit=500):
        facts = dict(case.source_payload.get("incidentFacts") or {})
        if str(facts.get("policyType") or "").casefold() != "local-in-policy6":
            continue
        opened = investigate.start("", None, None, case.case_id, auto_started=True)
        investigate.complete(str(opened["session_id"]))
        return case.case_id
    return None


def _session_snapshots() -> list[dict[str, Any]]:
    return investigate._session_store().recent(limit=100)


def run() -> dict[str, Any]:
    units: list[str] = []
    markers: list[Path] = []
    acceptance_memory = TieredMemoryStore(enabled=True)
    investigate._live_memory_store = lambda: acceptance_memory
    investigate._operational_context = lambda *_args: {}
    investigate._device_profile_anomaly_types = lambda *_args: []
    codex = CodexCLIClient()
    investigate._client = lambda *_args, **_kwargs: codex
    remediation.DEFAULT_BAKE_IN = BakeIn(
        window_seconds=3.0,
        stability_window_seconds=3.0,
        interval_seconds=1.0,
        grace_seconds=0.5,
        consecutive_bad=1,
        success_consecutive=2,
    )
    stop_state = remediation.emergency_stop().resume(
        "business-value-acceptance",
        "controlled loopback acceptance run",
    )
    evidence: dict[str, Any] = {
        "run_id": RUN_ID,
        "emergency_stop": stop_state.to_dict(),
    }
    try:
        evidence["ipv6_takeover_case_id"] = _refresh_ipv6_takeover()

        # Keep the targets outside the autonomous sentinel's watched prefixes.
        # The acceptance driver must be the only writer; otherwise an
        # independent recovery loop can restart a target between paired reads.
        recover_unit = f"bvaccept-recover-{RUN_ID[-6:]}.service"
        recover_port = _free_port()
        marker = Path("/run") / f"{recover_unit}.first-start"
        marker.unlink(missing_ok=True)
        markers.append(marker)
        units.append(recover_unit)
        _start_unit(recover_unit, mode="fail-once", port=recover_port, state_file=marker)
        first = _case(
            source_id=f"{RUN_ID}:recover:first",
            unit=recover_unit,
            summary="受管端点停止响应，调查本机可用性故障",
        )
        first_result = investigation_cases.auto_start_pending_cases(
            main._case_repository(), limit=1, case_ids={first.case_id}
        )[0]
        _wait_state(recover_unit, {"active"})

        _inject_recurrence_failure(recover_unit, recover_port)
        second = _case(
            source_id=f"{RUN_ID}:recover:repeat",
            unit=recover_unit,
            summary="同一受管端点再次停止响应，调查复发故障",
        )
        second_result = investigation_cases.auto_start_pending_cases(
            main._case_repository(), limit=1, case_ids={second.case_id}
        )[0]
        _wait_state(recover_unit, {"active"})
        evidence["successful_recovery"] = {
            "first_case_id": first.case_id,
            "second_case_id": second.case_id,
            "first_outcome": dict(first_result.get("decision") or {}).get("readback"),
            "second_outcome": dict(second_result.get("decision") or {}).get("readback"),
            "pair": dict(second_result.get("memory_evaluation") or {}),
            "unit_state_after": _unit_state(recover_unit),
        }

        memory_unit = f"bvaccept-memory-{RUN_ID[-6:]}.service"
        memory_subject = f"managed-host-{RUN_ID[-6:]}"
        memory_port = _free_port()
        memory_marker = Path("/run") / f"{memory_unit}.first-start"
        memory_marker.unlink(missing_ok=True)
        markers.append(memory_marker)
        units.append(memory_unit)
        _start_unit(
            memory_unit,
            mode="fail-once",
            port=memory_port,
            state_file=memory_marker,
        )
        memory_first = _case(
            source_id=f"{RUN_ID}:memory:first",
            unit=memory_subject,
            summary="受管主机出现未知可用性退化，定位当前根因",
        )
        memory_first_result = investigation_cases.auto_start_pending_cases(
            main._case_repository(), limit=1, case_ids={memory_first.case_id}
        )[0]
        memory_first_recovery = investigate.remediate(
            str(memory_first_result["session_id"])
        )
        _wait_state(memory_unit, {"active"})

        _inject_recurrence_failure(memory_unit, memory_port)
        memory_second = _case(
            source_id=f"{RUN_ID}:memory:repeat",
            unit=memory_subject,
            summary="同一受管主机再次出现未知可用性退化，验证复发调查收益",
        )
        memory_second_result = investigation_cases.auto_start_pending_cases(
            main._case_repository(), limit=1, case_ids={memory_second.case_id}
        )[0]
        memory_report = dict(memory_second_result.get("memory_evaluation") or {})
        memory_second_recovery = investigate.remediate(
            str(memory_second_result["session_id"])
        )
        _wait_state(memory_unit, {"active"})
        evidence["recurrence_memory"] = {
            "first_case_id": memory_first.case_id,
            "second_case_id": memory_second.case_id,
            "first_readback": memory_first_recovery.get("decision", {}).get("readback"),
            "second_readback": memory_second_recovery.get("decision", {}).get("readback"),
            "pair": memory_report,
            "unit_state_after": _unit_state(memory_unit),
        }

        fail_unit = f"bvaccept-fail-{RUN_ID[-6:]}.service"
        fail_port = _free_port()
        units.append(fail_unit)
        _start_unit(fail_unit, mode="always-fail", port=fail_port)
        failed = _case(
            source_id=f"{RUN_ID}:recover:permanent",
            unit=fail_unit,
            summary="受管端点无法启动，验证失败停止路径",
        )
        failed_result = investigation_cases.auto_start_pending_cases(
            main._case_repository(), limit=1, case_ids={failed.case_id}
        )[0]
        evidence["failed_recovery"] = {
            "case_id": failed.case_id,
            "decision": failed_result.get("decision"),
            "unit_state_after": _unit_state(fail_unit),
        }
        _stop_unit(fail_unit)

        schema_unit = f"bvaccept-schema-{RUN_ID[-6:]}.service"
        schema_port = _free_port()
        units.append(schema_unit)
        _start_unit(schema_unit, mode="schema-mismatch", port=schema_port)
        health_url = f"http://127.0.0.1:{schema_port}/health"
        schema_case = _case(
            source_id=f"{RUN_ID}:open:schema",
            unit=schema_unit,
            summary=(
                "订单端点持续返回旧版结构；期望 schema v2，当前根因不在固定故障目录中。"
                f"只读健康地址为 {health_url}"
            ),
            facts={
                "healthUrl": health_url,
                "expectedSchemaVersion": "v2",
                "observedSchemaVersion": "v1",
            },
        )
        opened = investigate.start(
            "", None, None, schema_case.case_id, auto_started=True
        )
        before = investigate.complete(str(opened["session_id"]))
        if dict(before.get("decision") or {}).get("classification") != "open_root_required":
            raise RuntimeError(f"open-root precondition failed: {before.get('decision')}")
        open_result = investigate.analyze(
            str(opened["session_id"]), client_override=codex
        )
        evidence["open_root"] = {
            "case_id": schema_case.case_id,
            "session_id": opened["session_id"],
            "decision": open_result.get("decision"),
            "citations": open_result.get("citations"),
            "codex_calls": codex.calls,
        }
        evidence["codex_call_count"] = len(codex.calls)

        cases = [
            case.as_dict()
            for case in main._case_repository().list(limit=500)
            if str(case.source_payload.get("acceptanceRunId") or "") == RUN_ID
        ]
        accepted_case_ids = {str(case.get("caseId") or "") for case in cases}
        sessions = [
            session for session in _session_snapshots()
            if str(session.get("case_id") or "") in accepted_case_ids
        ]
        report = evaluate_business_value(cases, sessions)
        evidence["business_value"] = report
        evidence["case_count"] = len(cases)
        evidence["session_count"] = len(sessions)
        return evidence
    finally:
        for unit in reversed(units):
            _stop_unit(unit)
        for marker in markers:
            marker.unlink(missing_ok=True)


def main_cli() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "benchmark_results" / "business_value_acceptance_latest.json",
    )
    args = parser.parse_args()
    result = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result["business_value"], ensure_ascii=False, indent=2))
    return 0 if result["business_value"].get("allProven") else 2


if __name__ == "__main__":
    raise SystemExit(main_cli())

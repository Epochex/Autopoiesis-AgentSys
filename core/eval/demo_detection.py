"""Fault-injection + detection-verification demo harness.

For each known network-RCA incident type this harness:

  1. INJECTS a deterministic, realistic FortiGate syslog sequence
     (``domains.network_rca.fault_injection``);
  2. DERIVES the ``real_window_stats`` aggregates by parsing those emitted lines
     (nothing hand-written into the stats);
  3. runs the REAL existing detection path unchanged — ``RealSyslogAdapter`` ->
     read-only skill library -> ``SkillAttentionController`` -> the reasoner in
     ``domains.network_rca.reasoner`` -> ``core.verifier.Verifier`` — via the
     production ``build_network_rca_orchestrator``; and
  4. ASSERTS the system localizes the CORRECT root cause, reporting detection
     outcome, the probe count, latency and the verifier verdict.

The verdict is produced entirely by the real reasoner; ``expected_root_cause_key``
is only the assertion target, never an input to detection. Two NEGATIVE CONTROLS
run the brute-force and port-probe operator queries against a clean (fault-free)
window and require that the fault is NOT localized — proof the detector tracks the
injected data rather than replaying a script.

    python3 -m core.eval.demo_detection

Exit code 0 iff every claimed detection localizes its cause AND both negative
controls stay clean; non-zero otherwise (an honest fail, never a faked pass).

Uses the deterministic ``rule`` reasoner (``domains.network_rca.reasoner.build_diagnosis``),
the same offline detection path ``examples/benchmarks.py`` runs. The ``llm`` reasoner
exists but needs an OpenAI-compatible endpoint and is intentionally not exercised
here (no network is contacted).
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root importable

from domains.network_rca.factory import build_network_rca_orchestrator  # noqa: E402
from domains.network_rca.fault_injection import (  # noqa: E402
    InjectedIncident,
    inject_all,
    inject_clean_baseline,
    inject_admin_bruteforce_lockout,
    inject_device_port_probe,
)


@dataclass
class DetectionResult:
    incident_type: str
    expected_key: str
    detected_key: str
    matched: bool
    verifier_passed: bool
    probes: int
    exposed_skills: list[str]
    latency_ms: float
    confidence: float
    n_lines: int
    signal: str


def _signal(incident_type: str, stats: dict) -> str:
    """A short, human-readable summary of the statistic that drives this diagnosis."""
    p = stats.get("performance_sample", {}) or {}
    table = {
        "admin_bruteforce_lockout": f"{stats.get('admin_login_failed', 0)} failed logins / "
        f"{stats.get('admin_login_failed_distinct_src', 0)} src IPs / "
        f"{stats.get('admin_login_disabled_lockouts', 0)} lockouts",
        "internal_policy_deny_flood": f"{stats.get('deny_count', 0)} denies / "
        f"top src {[s for s, _ in stats.get('deny_top_src', [])][:2]}",
        "device_port_probe": f"{stats.get('device_service_port_deny', 0)} DVR-port denies / "
        f"ports {[p for p, _ in stats.get('device_service_port_top', [])]}",
        "dhcp_health": f"{stats.get('dhcp_ack_count', 0)} DHCP ACK / {stats.get('dhcp_statistics_count', 0)} stats",
        "security_posture": f"{stats.get('fortigate_update_succeeded', 0)} FortiGuard updates",
        "session_clash": f"{stats.get('session_clash', 0)} session-clash events",
        "firewall_resource_headroom": f"CPU {p.get('cpu', '?')}% / MEM {p.get('mem', '?')}% / "
        f"{p.get('totalsession', '?')} sessions",
    }
    return table.get(incident_type, "")


def run_detection(incident: InjectedIncident, *, stats_override: dict | None = None) -> DetectionResult:
    """Drive ONE injected incident through the real detection pipeline and score it."""
    stats = stats_override if stats_override is not None else incident.window_stats()
    with TemporaryDirectory() as tmp_dir:
        stats_path = Path(tmp_dir) / "real_window_stats.json"
        stats_path.write_text(json.dumps(stats), encoding="utf-8")
        ledger_path = Path(tmp_dir) / "ledger.jsonl"

        orchestrator = build_network_rca_orchestrator(
            ledger_path,
            reasoner_mode="rule",
            data_source="real",
            real_stats_path=stats_path,
        )

        start = time.perf_counter()
        diagnosis, report = orchestrator.diagnose(incident.case)
        latency_ms = (time.perf_counter() - start) * 1000.0

        events = orchestrator.ledger.replay()
        exposed: list[str] = []
        probes = 0
        for event in events:
            if event.kind == "skills_exposed":
                exposed = list(event.payload.get("skills", []))
            elif event.kind == "cost_observed":
                probes = int(event.payload.get("tool_calls", 0))

    return DetectionResult(
        incident_type=incident.incident_type,
        expected_key=incident.expected_root_cause_key,
        detected_key=diagnosis.root_cause_key,
        matched=diagnosis.root_cause_key == incident.expected_root_cause_key,
        verifier_passed=report.passed,
        probes=probes,
        exposed_skills=exposed,
        latency_ms=latency_ms,
        confidence=diagnosis.confidence,
        n_lines=len(incident.syslog_lines),
        signal=_signal(incident.incident_type, stats),
    )


def _print_table(results: list[DetectionResult]) -> None:
    header = f"{'incident':<28}{'lines':>7}{'probes':>7}{'ms':>7}  {'detected':<34}{'ok':>4}{'ver':>5}"
    print(header)
    print("-" * len(header))
    for r in results:
        mark = "PASS" if r.matched else "FAIL"
        ver = "ok" if r.verifier_passed else "X"
        print(
            f"{r.incident_type:<28}{r.n_lines:>7}{r.probes:>7}{r.latency_ms:>7.1f}  "
            f"{r.detected_key:<34}{mark:>4}{ver:>5}"
        )
        print(f"    signal: {r.signal}")
        if not r.matched:
            print(f"    !! expected '{r.expected_key}' but reasoner returned '{r.detected_key}'")


def main() -> int:
    print("=" * 78)
    print("FAULT INJECTION -> REAL DETECTION (rule reasoner, offline, no network)")
    print("=" * 78)
    print("Each row: inject realistic syslog -> derive stats by parsing -> run the")
    print("existing RealSyslogAdapter + skills + reasoner + verifier pipeline.\n")

    incidents = inject_all()
    results = [run_detection(incident) for incident in incidents]
    _print_table(results)

    detected = sum(1 for r in results if r.matched)
    verified = sum(1 for r in results if r.matched and r.verifier_passed)
    print(f"\nGENUINELY DETECTED END-TO-END: {detected}/{len(results)}  "
          f"(verifier-clean: {verified}/{len(results)})")

    # ── negative controls: the fault query against a fault-free window ──────────
    print("\n" + "-" * 78)
    print("NEGATIVE CONTROLS (same operator query, clean fault-free window):")
    clean = inject_clean_baseline()
    from domains.network_rca.fault_injection import aggregate_window_stats

    clean_stats = aggregate_window_stats(clean)
    controls_ok = True
    for factory in (inject_admin_bruteforce_lockout, inject_device_port_probe):
        incident = factory()
        control = run_detection(incident, stats_override=clean_stats)
        stayed_clean = control.detected_key != incident.expected_root_cause_key
        controls_ok = controls_ok and stayed_clean
        status = "OK (not localized)" if stayed_clean else "LEAK (localized on clean data!)"
        print(f"  {incident.incident_type:<28} -> detected='{control.detected_key}'  [{status}]")

    all_ok = detected == len(results) and verified == len(results) and controls_ok
    print("\n" + "=" * 78)
    print("RESULT:", "ALL DETECTIONS REAL AND CORRECT" if all_ok else "SOME DETECTIONS FAILED (see above)")
    print("=" * 78)
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

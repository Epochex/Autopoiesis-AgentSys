"""Evaluate durable, multi-step investigation traces.

This module scores exported investigation traces.  It does not simulate an
incident, generate a diagnosis, or award business value for a component being
called.  A trace earns credit only when it preserves case state across steps,
waits for decisive evidence, revises a contradicted hypothesis, survives a
process-generation change through a checkpoint, and closes on the labelled
root cause.

Input is a JSON document with ``cases``.  Every case declares the decisive
evidence step and may declare a false lead plus the step that contradicts it.
Each case contains paired ``no_memory`` and ``with_memory`` runs.  The report
keeps outcome, safety, continuity, and cost metrics separate.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any, Iterable, Mapping, Sequence


VALID_MODES = {"no_memory", "with_memory"}
VALID_EVENT_TYPES = {"evidence", "probe", "hypothesis", "wait", "close"}


@dataclass(frozen=True)
class RunMetrics:
    case_id: str
    mode: str
    stable_root_cause: bool
    premature_close: bool
    contradiction_revised: bool | None
    restart_recovered: bool | None
    state_continuous: bool
    repeated_probe_count: int
    probe_count: int
    steps_to_stable_root: int | None
    memory_used: bool


def _require_int(value: Any, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def _validate_steps(steps: Any, *, case_id: str, mode: str) -> list[dict[str, Any]]:
    if not isinstance(steps, list) or not steps:
        raise ValueError(f"{case_id}/{mode}: steps must be a non-empty list")
    rows = [dict(row) for row in steps]
    sequences = [_require_int(row.get("sequence"), "sequence", minimum=1) for row in rows]
    if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
        raise ValueError(f"{case_id}/{mode}: sequence values must be unique and increasing")
    for row in rows:
        event_type = str(row.get("event_type", ""))
        if event_type not in VALID_EVENT_TYPES:
            raise ValueError(f"{case_id}/{mode}: unsupported event_type {event_type!r}")
        _require_int(row.get("state_version"), "state_version", minimum=1)
        _require_int(row.get("process_generation", 1), "process_generation", minimum=1)
        if event_type == "probe" and not str(row.get("probe_key", "")).strip():
            raise ValueError(f"{case_id}/{mode}: probe events require probe_key")
        if event_type in {"hypothesis", "close"} and not str(row.get("hypothesis", "")).strip():
            raise ValueError(f"{case_id}/{mode}: {event_type} events require hypothesis")
    return rows


def _state_continuity(steps: Sequence[Mapping[str, Any]]) -> tuple[bool, bool | None]:
    versions = [int(row["state_version"]) for row in steps]
    monotonic = all(current >= previous for previous, current in zip(versions, versions[1:]))
    generation_changes: list[int] = []
    for index in range(1, len(steps)):
        if int(steps[index]["process_generation"]) != int(steps[index - 1]["process_generation"]):
            generation_changes.append(index)
    if not generation_changes:
        return monotonic, None
    recovered = monotonic and all(
        bool(steps[index].get("checkpoint_restored"))
        and int(steps[index]["state_version"]) >= int(steps[index - 1]["state_version"])
        for index in generation_changes
    )
    return monotonic and recovered, recovered


def _stable_hypothesis_step(
    steps: Sequence[Mapping[str, Any]], expected_root: str, decisive_step: int
) -> int | None:
    hypotheses = [
        (int(row["sequence"]), str(row["hypothesis"]))
        for row in steps
        if row["event_type"] in {"hypothesis", "close"}
    ]
    for index, (sequence, hypothesis) in enumerate(hypotheses):
        if sequence < decisive_step or hypothesis != expected_root:
            continue
        if all(later == expected_root for _, later in hypotheses[index:]):
            return sequence
    return None


def evaluate_run(case: Mapping[str, Any], run: Mapping[str, Any]) -> RunMetrics:
    case_id = str(case.get("case_id", "")).strip()
    expected_root = str(case.get("expected_root_cause", "")).strip()
    if not case_id or not expected_root:
        raise ValueError("each case requires case_id and expected_root_cause")
    decisive_step = _require_int(case.get("decisive_evidence_step"), "decisive_evidence_step", minimum=1)
    mode = str(run.get("mode", ""))
    if mode not in VALID_MODES:
        raise ValueError(f"{case_id}: mode must be one of {sorted(VALID_MODES)}")
    steps = _validate_steps(run.get("steps"), case_id=case_id, mode=mode)

    close_rows = [row for row in steps if row["event_type"] == "close"]
    close_row = close_rows[-1] if close_rows else None
    close_step = int(close_row["sequence"]) if close_row else None
    close_root = str(close_row["hypothesis"]) if close_row else ""
    stable_step = _stable_hypothesis_step(steps, expected_root, decisive_step)
    stable_root = bool(close_row and close_root == expected_root and stable_step is not None)
    premature_close = bool(close_step is not None and close_step < decisive_step)

    false_root = str(case.get("false_lead_root_cause", "")).strip()
    contradiction_step_raw = case.get("contradiction_step")
    contradiction_revised: bool | None = None
    if false_root and contradiction_step_raw is not None:
        contradiction_step = _require_int(contradiction_step_raw, "contradiction_step", minimum=1)
        had_false_lead = any(
            row["event_type"] == "hypothesis"
            and int(row["sequence"]) < contradiction_step
            and str(row["hypothesis"]) == false_root
            for row in steps
        )
        revised_after = any(
            row["event_type"] in {"hypothesis", "close"}
            and int(row["sequence"]) >= contradiction_step
            and str(row["hypothesis"]) == expected_root
            for row in steps
        )
        contradiction_revised = had_false_lead and revised_after and stable_root

    state_continuous, restart_recovered = _state_continuity(steps)
    probes = [str(row["probe_key"]) for row in steps if row["event_type"] == "probe"]
    repeated_probes = sum(count - 1 for count in Counter(probes).values() if count > 1)
    memory_used = any(bool(row.get("memory_ids")) for row in steps)
    return RunMetrics(
        case_id=case_id,
        mode=mode,
        stable_root_cause=stable_root,
        premature_close=premature_close,
        contradiction_revised=contradiction_revised,
        restart_recovered=restart_recovered,
        state_continuous=state_continuous,
        repeated_probe_count=repeated_probes,
        probe_count=len(probes),
        steps_to_stable_root=(stable_step - 1 if stable_step is not None else None),
        memory_used=memory_used,
    )


def _rate(values: Iterable[bool]) -> float | None:
    rows = list(values)
    return round(sum(rows) / len(rows), 6) if rows else None


def _mean(values: Iterable[int | float | None]) -> float | None:
    rows = [float(value) for value in values if value is not None]
    return round(mean(rows), 6) if rows else None


def _summarize(rows: Sequence[RunMetrics]) -> dict[str, Any]:
    return {
        "run_count": len(rows),
        "stable_root_cause_rate": _rate(row.stable_root_cause for row in rows),
        "premature_close_rate": _rate(row.premature_close for row in rows),
        "contradiction_revision_rate": _rate(
            row.contradiction_revised for row in rows if row.contradiction_revised is not None
        ),
        "restart_recovery_rate": _rate(
            row.restart_recovered for row in rows if row.restart_recovered is not None
        ),
        "state_continuity_rate": _rate(row.state_continuous for row in rows),
        "mean_probe_count": _mean(row.probe_count for row in rows),
        "mean_repeated_probe_count": _mean(row.repeated_probe_count for row in rows),
        "mean_steps_to_stable_root": _mean(row.steps_to_stable_root for row in rows),
    }


def evaluate_temporal_cases(payload: Mapping[str, Any]) -> dict[str, Any]:
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("payload must contain a non-empty cases list")

    metrics: list[RunMetrics] = []
    paired_case_ids: list[str] = []
    for raw_case in cases:
        case = dict(raw_case)
        runs = case.get("runs")
        if not isinstance(runs, list) or not runs:
            raise ValueError(f"{case.get('case_id', '<unknown>')}: runs must be non-empty")
        case_rows = [evaluate_run(case, dict(run)) for run in runs]
        modes = [row.mode for row in case_rows]
        if len(modes) != len(set(modes)):
            raise ValueError(f"{case.get('case_id')}: duplicate mode")
        if VALID_MODES.issubset(modes):
            paired_case_ids.append(str(case["case_id"]))
        metrics.extend(case_rows)

    by_mode = {
        mode: _summarize([row for row in metrics if row.mode == mode])
        for mode in sorted(VALID_MODES)
    }
    full_pairs = [
        case_id
        for case_id in paired_case_ids
        if {row.mode for row in metrics if row.case_id == case_id} == VALID_MODES
    ]
    no_memory = {row.case_id: row for row in metrics if row.mode == "no_memory"}
    with_memory = {row.case_id: row for row in metrics if row.mode == "with_memory"}
    paired = [(no_memory[case_id], with_memory[case_id]) for case_id in full_pairs]
    ablation = {
        "paired_case_count": len(paired),
        "stable_root_cause_rate_delta": _rate(b.stable_root_cause for _, b in paired),
        "stable_root_cause_rate_baseline": _rate(a.stable_root_cause for a, _ in paired),
        "premature_close_rate_delta": (
            round(
                float(_rate(b.premature_close for _, b in paired) or 0.0)
                - float(_rate(a.premature_close for a, _ in paired) or 0.0),
                6,
            )
            if paired else None
        ),
        "mean_probe_count_delta": _mean(b.probe_count - a.probe_count for a, b in paired),
        "mean_repeated_probe_count_delta": _mean(
            b.repeated_probe_count - a.repeated_probe_count for a, b in paired
        ),
        "mean_steps_to_stable_root_delta": _mean(
            (b.steps_to_stable_root - a.steps_to_stable_root)
            for a, b in paired
            if a.steps_to_stable_root is not None and b.steps_to_stable_root is not None
        ),
    }
    if ablation["stable_root_cause_rate_delta"] is not None:
        ablation["stable_root_cause_rate_delta"] = round(
            float(ablation["stable_root_cause_rate_delta"])
            - float(ablation["stable_root_cause_rate_baseline"] or 0.0),
            6,
        )

    return {
        "schema_version": 1,
        "evaluation_kind": "temporal_investigation_trace",
        "scope": (
            "scores exported multi-step case traces; it does not measure model quality, "
            "live data integration, or action safety unless those events are present"
        ),
        "by_mode": by_mode,
        "memory_ablation": ablation,
        "runs": [row.__dict__ for row in metrics],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="JSON trace bundle")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    result = evaluate_temporal_cases(json.loads(args.input.read_text(encoding="utf-8")))
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["RunMetrics", "evaluate_run", "evaluate_temporal_cases"]

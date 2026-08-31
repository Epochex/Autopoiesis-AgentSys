"""Acceptance gate for two executions of the same live investigation case."""

from __future__ import annotations

from typing import Any, Mapping


def compare_investigation_pair(
    control: Mapping[str, Any],
    treatment: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare no-memory and memory-enabled traces without inventing a win."""
    control_roots = tuple(control.get("confirmed_roots") or ())
    treatment_roots = tuple(treatment.get("confirmed_roots") or ())
    same_root = bool(control_roots) and control_roots == treatment_roots
    control_step = control.get("steps_to_first_confirmation")
    treatment_step = treatment.get("steps_to_first_confirmation")
    faster = (
        isinstance(control_step, int)
        and isinstance(treatment_step, int)
        and treatment_step < control_step
    )
    same_coverage = set(control.get("candidate_probes") or ()) == set(
        treatment.get("candidate_probes") or ()
    )
    no_extra_unscoped_context = int(treatment.get("unscoped_context_count") or 0) <= int(
        control.get("unscoped_context_count") or 0
    )
    control_outputs = dict(
        control.get("decisive_probe_output_fingerprints")
        or control.get("probe_output_fingerprints")
        or {}
    )
    treatment_outputs = dict(
        treatment.get("decisive_probe_output_fingerprints")
        or treatment.get("probe_output_fingerprints")
        or {}
    )
    common_commands = set(control_outputs).intersection(treatment_outputs)
    comparable_inputs = bool(common_commands) and all(
        control_outputs[command] == treatment_outputs[command]
        for command in common_commands
    )
    memory_influenced = bool(treatment.get("memory_influenced_order"))
    control_probe_count = control.get("probe_count")
    treatment_probe_count = treatment.get("probe_count")
    fewer_probes = bool(
        isinstance(control_probe_count, int)
        and isinstance(treatment_probe_count, int)
        and treatment_probe_count < control_probe_count
    )
    control_elapsed = control.get("elapsed_ms")
    treatment_elapsed = treatment.get("elapsed_ms")
    wall_time_measurable = bool(
        isinstance(control_elapsed, (int, float))
        and isinstance(treatment_elapsed, (int, float))
        and max(float(control_elapsed), float(treatment_elapsed)) >= 50.0
    )
    lower_wall_time = bool(
        wall_time_measurable
        and treatment_elapsed < control_elapsed
    )
    proven = bool(
        same_root
        and same_coverage
        and faster
        and fewer_probes
        and wall_time_measurable
        and lower_wall_time
        and no_extra_unscoped_context
        and comparable_inputs
        and memory_influenced
    )
    return {
        "same_confirmed_root": same_root,
        "same_probe_coverage": same_coverage,
        "faster_confirmation": faster,
        "fewer_executed_probes": fewer_probes,
        "probe_count_delta": (
            control_probe_count - treatment_probe_count
            if isinstance(control_probe_count, int) and isinstance(treatment_probe_count, int)
            else None
        ),
        "lower_wall_time": lower_wall_time,
        "wall_time_measurable": wall_time_measurable,
        "elapsed_ms_delta": (
            round(float(control_elapsed) - float(treatment_elapsed), 3)
            if isinstance(control_elapsed, (int, float))
            and isinstance(treatment_elapsed, (int, float))
            else None
        ),
        "confirmation_step_delta": (
            control_step - treatment_step
            if isinstance(control_step, int) and isinstance(treatment_step, int)
            else None
        ),
        "no_extra_unscoped_context": no_extra_unscoped_context,
        "comparable_probe_outputs": comparable_inputs,
        "compared_probe_count": len(common_commands),
        "memory_influenced_order": memory_influenced,
        "business_value_proven": proven,
        "failure_reason": (
            None
            if proven
            else (
                "decisive probe outputs are missing or changed between executions"
                if not comparable_inputs
                else (
                    "wall time is missing or below the measurable floor"
                    if not wall_time_measurable
                    else "memory did not confirm the same root earlier with fewer probes"
                )
            )
        ),
    }


__all__ = ["compare_investigation_pair"]

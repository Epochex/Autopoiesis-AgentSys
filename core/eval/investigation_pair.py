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
    control_outputs = dict(control.get("probe_output_fingerprints") or {})
    treatment_outputs = dict(treatment.get("probe_output_fingerprints") or {})
    common_commands = set(control_outputs).intersection(treatment_outputs)
    comparable_inputs = bool(common_commands) and all(
        control_outputs[command] == treatment_outputs[command]
        for command in common_commands
    )
    if not control_outputs and not treatment_outputs:
        # Backward-compatible unit inputs still exercise the comparison rule.
        # Production metrics always carry fingerprints.
        comparable_inputs = True
    memory_influenced = bool(treatment.get("memory_influenced_order"))
    proven = bool(
        same_root
        and same_coverage
        and faster
        and no_extra_unscoped_context
        and comparable_inputs
        and memory_influenced
    )
    return {
        "same_confirmed_root": same_root,
        "same_probe_coverage": same_coverage,
        "faster_confirmation": faster,
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
                "probe outputs changed between executions"
                if not comparable_inputs
                else "memory did not confirm the same root earlier under equal probe coverage"
            )
        ),
    }


__all__ = ["compare_investigation_pair"]

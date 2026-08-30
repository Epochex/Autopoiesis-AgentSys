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
        "memory_influenced_order": bool(treatment.get("memory_influenced_order")),
        "business_value_proven": bool(
            same_root and same_coverage and faster and no_extra_unscoped_context
        ),
        "failure_reason": (
            None
            if same_root and same_coverage and faster and no_extra_unscoped_context
            else "memory did not confirm the same root earlier under equal probe coverage"
        ),
    }


__all__ = ["compare_investigation_pair"]

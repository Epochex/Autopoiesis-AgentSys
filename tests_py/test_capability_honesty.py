"""Executable checks that public capability claims have production evidence."""
from __future__ import annotations

import ast
from pathlib import Path

from core.evolve.observatory import (
    CAPABILITIES,
    CAPABILITY_STATUS,
    PRODUCTION_CALL_SITES,
    CapabilityEvidence,
)
from core.orchestrator.evolving_service import memory_retention_wiring


ROOT = Path(__file__).resolve().parents[1]


def _call_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return ""


def _matches(evidence: CapabilityEvidence) -> bool:
    path = ROOT / evidence.path
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    if evidence.kind == "dict_key":
        return any(
            isinstance(node, ast.Dict)
            and evidence.name in {
                key.value
                for key in node.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
            for node in ast.walk(tree)
        )
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node) != evidence.name:
            continue
        strings = {
            item.value
            for item in ast.walk(node)
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        }
        if evidence.marker is not None and evidence.marker not in strings:
            continue
        if evidence.keyword is not None:
            keyword = next(
                (item for item in node.keywords if item.arg == evidence.keyword), None
            )
            if keyword is None:
                continue
            if evidence.keyword_value is not None and not (
                isinstance(keyword.value, ast.Constant)
                and keyword.value.value is evidence.keyword_value
            ):
                continue
        return True
    return False


def test_every_true_production_wiring_flag_has_a_real_call_site():
    """A True wiring claim must be rediscoverable from non-test production code."""
    for capability, status in CAPABILITY_STATUS.items():
        if not status["production_wired"]:
            continue
        proofs = PRODUCTION_CALL_SITES[capability]
        assert proofs, f"{capability} claims production wiring without evidence locators"
        assert any(
            all(_matches(evidence) for evidence in proof) for proof in proofs
        ), f"{capability} claims production wiring without a matching production call"


def test_legacy_and_service_capability_views_share_one_source():
    expected_legacy = {
        "decay_wired": CAPABILITY_STATUS["decay"]["production_wired"],
        "eviction_wired": CAPABILITY_STATUS["eviction"]["production_wired"],
        "contradiction_quarantine_wired": CAPABILITY_STATUS[
            "contradiction_quarantine"
        ]["production_wired"],
        "conflict_update_wired": CAPABILITY_STATUS["conflict_update"][
            "production_wired"
        ],
        "retrieval_scores": CAPABILITY_STATUS["retrieval_scoring"]["production_wired"],
        "context_drop_reason": CAPABILITY_STATUS["context_drop_provenance"][
            "production_wired"
        ],
        "update_text_mutation": CAPABILITY_STATUS["update_text_mutation"][
            "implemented"
        ],
    }
    assert CAPABILITIES == expected_legacy

    default_retention = memory_retention_wiring()
    for name in ("decay", "eviction"):
        assert {
            key: default_retention[name][key]
            for key in ("implemented", "production_wired")
        } == CAPABILITY_STATUS[name]
    assert default_retention["decay"]["configured"] is True
    assert default_retention["eviction"]["configured"] is False
    assert memory_retention_wiring(memory_budget=10)["eviction"]["configured"] is True


def test_unwired_implemented_mechanism_is_reported_in_both_dimensions():
    contradiction = CAPABILITY_STATUS["contradiction_quarantine"]
    assert contradiction == {"implemented": True, "production_wired": False}


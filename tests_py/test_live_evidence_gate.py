from __future__ import annotations

from frontend.gateway.app.evidence_gate import (
    evidence_fact,
    verify_graph_claims,
    verify_pair_claims,
)


def _edge(src: str, dst: str) -> dict:
    fact = {"src": src, "dst": dst, "kind": "observed", "observed": True}
    return evidence_fact("graph_edge", fact, subjects=[src, dst], pair=(src, dst))


def test_graph_corridor_rejects_known_ips_without_supporting_edge() -> None:
    edge = _edge("10.0.0.1", "10.0.0.2")

    patterns, corridors, rejected, errors = verify_graph_claims(
        [],
        [
            {
                "src": "10.0.0.1",
                "dst": "10.0.0.3",
                "path": ["10.0.0.1", "10.0.0.3"],
                "evidence_ids": [edge["evidence_id"]],
            }
        ],
        [edge],
        {"10.0.0.1", "10.0.0.2", "10.0.0.3"},
    )

    assert patterns == []
    assert corridors == []
    assert len(rejected["corridors"]) == 1
    assert errors[0]["reason"] == "corridor is not backed by every edge in its path"


def test_graph_corridor_rejects_unknown_evidence_id() -> None:
    edge = _edge("10.0.0.1", "10.0.0.2")

    _, corridors, rejected, errors = verify_graph_claims(
        [],
        [
            {
                "src": "10.0.0.1",
                "dst": "10.0.0.2",
                "evidence_ids": ["ev-does-not-exist"],
            }
        ],
        [edge],
        {"10.0.0.1", "10.0.0.2"},
    )

    assert corridors == []
    assert len(rejected["corridors"]) == 1
    assert errors[0]["reason"] == "corridor cites an unknown evidence_id"


def test_graph_corridor_accepts_cited_multihop_path() -> None:
    first = _edge("10.0.0.1", "10.0.0.2")
    second = _edge("10.0.0.2", "10.0.0.3")

    _, corridors, rejected, errors = verify_graph_claims(
        [],
        [
            {
                "src": "10.0.0.1",
                "dst": "10.0.0.3",
                "path": ["10.0.0.1", "10.0.0.2", "10.0.0.3"],
                "evidence_ids": [first["evidence_id"], second["evidence_id"]],
            }
        ],
        [first, second],
        {"10.0.0.1", "10.0.0.2", "10.0.0.3"},
    )

    assert len(corridors) == 1
    assert corridors[0]["evidenceIds"] == [first["evidence_id"], second["evidence_id"]]
    assert rejected == {"patterns": [], "corridors": []}
    assert errors == []


def test_pair_claim_requires_evidence_for_the_exact_pair() -> None:
    fact = evidence_fact(
        "host_relationship",
        {"source": "10.0.0.1", "target": "10.0.0.2", "shared_ports": [443]},
        subjects=["10.0.0.1", "10.0.0.2"],
        pair=("10.0.0.1", "10.0.0.2"),
    )

    verified, rejected, errors = verify_pair_claims(
        [
            {
                "src": "10.0.0.1",
                "dst": "10.0.0.2",
                "relation": "shared TLS target",
                "evidence_ids": [fact["evidence_id"]],
            }
        ],
        [fact],
        source_field="src",
        target_field="dst",
        field="links",
    )

    assert len(verified) == 1
    assert rejected == []
    assert errors == []

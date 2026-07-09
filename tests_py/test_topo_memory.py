from __future__ import annotations

from core.memory.topo_graph import TopoGraphMemory


def _record(rid, entity, entity_type="device", relations=None, evidence_ids=None):
    return {
        "id": rid,
        "entity": entity,
        "entity_type": entity_type,
        "relations": relations or [],
        "attrs": {},
        "provenance": {
            "evidence_ids": evidence_ids if evidence_ids is not None else [f"ev-{rid}"],
            "source": "test",
            "ts": "2026-07-09T00:00:00Z",
        },
    }


def test_add_query_and_neighbours():
    graph = TopoGraphMemory()
    graph.add_record(_record("dev-a", "A", relations=[{"target": "link-ab", "type": "topology"}]))
    graph.add_record(_record("link-ab", "link-ab", "link", relations=[{"target": "B", "type": "topology"}]))
    graph.add_record(_record("dev-b", "B"))

    assert [r.id for r in graph.query_by_entity("A")] == ["dev-a"]
    assert {r.id for r in graph.neighbors("A")} == {"link-ab"}
    assert len(graph.all_records()) == 3


def test_path_traversal_reaches_multihop_topology_records():
    graph = TopoGraphMemory([
        _record("alert-a", "A", "alert", relations=[{"target": "link-ab", "type": "topology"}]),
        _record("link-ab", "link-ab", "link", relations=[{"target": "B", "type": "topology"}]),
        _record("rc-b", "root-on-b", "root_cause", relations=[{"target": "B", "type": "topology"}]),
    ])

    assert "rc-b" not in {r.id for r in graph.query_by_path("A", "topology", 1)}
    assert "rc-b" in {r.id for r in graph.query_by_path("A", "topology", 3)}


def test_unprovenanced_record_is_flagged_not_grounded():
    graph = TopoGraphMemory([
        _record("grounded", "A"),
        _record("unprovenanced", "A", evidence_ids=[]),
    ])

    assert graph.is_grounded("grounded") is True
    assert graph.is_grounded("unprovenanced") is False
    assert graph.is_grounded("missing") is False

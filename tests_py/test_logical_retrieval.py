from __future__ import annotations

import json
from pathlib import Path

from core.memory.logical_retrieval import logical_retrieve, naive_similarity_retrieve
from core.memory.topo_graph import TopoGraphMemory


_FIXTURE = Path("domains/network_rca/fixtures/topo_incidents.json")


def _fixture():
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def test_logical_retrieval_finds_multihop_root_cause_where_naive_does_not():
    fixture = _fixture()
    graph = TopoGraphMemory(fixture["records"])
    case = next(q for q in fixture["queries"] if q["id"] == "q-r230-multihop-root-cause")

    logical_ids = [r.id for r in logical_retrieve(case["query"], graph, case["k"])]
    naive_ids = [r.id for r in naive_similarity_retrieve(case["query"], fixture["records"], case["k"])]

    assert logical_ids == ["rc-fortilink-broadcast-storm"]
    assert "rc-fortilink-broadcast-storm" not in naive_ids


def test_logical_retrieval_never_returns_unprovenanced_records_as_grounded():
    fixture = _fixture()
    graph = TopoGraphMemory(fixture["records"])
    query = {
        "entities": ["R230"],
        "relation": "topology",
        "intent": "root cause R230 syslog lag disk full dropped messages",
    }

    logical_ids = [r.id for r in logical_retrieve(query, graph, 5)]
    naive_ids = [r.id for r in naive_similarity_retrieve(query, fixture["records"], 3)]

    assert "rc-r230-disk-full-unprovenanced" not in logical_ids
    assert "rc-r230-disk-full-unprovenanced" in naive_ids

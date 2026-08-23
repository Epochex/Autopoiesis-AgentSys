"""Deterministic boundary coverage for the zero-dependency memory core."""
from __future__ import annotations

import pytest

from core.evolve import route, supersede, utility_evict, utility_scores
from core.memory.bm25 import BM25Index, tokenize
from core.memory.rrf import rrf_fuse
from core.memory.store import MemoryRecord, TieredMemoryStore


_EMPTY_RETRIEVAL = {
    "episodic": [],
    "semantic": [],
    "procedural": [],
    "asset_profile": [],
}


def _record(
    memory_id: str,
    *,
    tags: list[str] | None = None,
    assets: list[str] | None = None,
    root: str = "carrier_down",
) -> MemoryRecord:
    return MemoryRecord(
        memory_id=memory_id,
        tier="episodic",
        text=f"{memory_id}: {root}",
        tags=[*(tags or []), f"root:{root}"],
        asset_ids=assets or [],
    )


def test_route_thresholds_are_inclusive_and_select_the_expected_band():
    store = TieredMemoryStore()
    existing = _record(
        "existing",
        tags=["carrier", "interface"],
        assets=["r230", "eno1"],
    )
    store.add(existing)

    # Candidate is a strict subset, so it contributes no new information. Its
    # similarity is exactly .5: .6 * (1/2 tag Jaccard) + .4 * (1/2 asset Jaccard).
    subset = _record("subset", tags=["carrier"], assets=["r230"])
    assert route(store, subset, update_thresh=0.5001).op == "ADD"
    at_update_boundary = route(store, subset, update_thresh=0.5, noop_thresh=0.5001)
    assert at_update_boundary.op == "UPDATE"
    assert at_update_boundary.similarity == pytest.approx(0.5)
    at_noop_boundary = route(store, subset, update_thresh=0.5, noop_thresh=0.5)
    assert at_noop_boundary.op == "NOOP"
    assert at_noop_boundary.target_id == "existing"


def test_rrf_breaks_an_exact_score_tie_by_document_id():
    # Each document appears once at rank one, so both have exactly 1 / (c + 1).
    assert rrf_fuse([["z-doc"], ["a-doc"]], 2) == ["a-doc", "z-doc"]


def test_utility_eviction_protects_structured_priors_even_below_budget():
    store = TieredMemoryStore()
    protected = [
        MemoryRecord(memory_id="manual-knowledge", tier="semantic", text="seed", tags=["seed"]),
        MemoryRecord(memory_id="machine-profile", tier="asset_profile", text="asset"),
        MemoryRecord(memory_id="family-summary", tier="semantic", text="insight", tags=["insight"]),
    ]
    disposable = [_record("episode-a"), _record("episode-b")]
    for record in [*protected, *disposable]:
        store.add(record)

    # Protected priors alone exceed the budget. The contract keeps them all and
    # evicts every unprotected record rather than violating prior protection.
    assert utility_scores(store).keys() == {"episode-a", "episode-b"}
    assert utility_evict(store, budget=1) == ["episode-a", "episode-b"]
    assert {record.memory_id for record in store.active()} == {
        "manual-knowledge",
        "machine-profile",
        "family-summary",
    }
    assert all(not record.quarantined for record in protected)


def test_supersede_keeps_an_idempotent_two_way_provenance_chain():
    store = TieredMemoryStore()
    old = _record("old", tags=["carrier"], assets=["r230"], root="carrier_down")
    replacement = _record("new", tags=["carrier"], assets=["r230"], root="sfp_fault")
    replacement.links.append("supporting-observation")
    store.add(old)
    operations: list[dict] = []

    assert supersede(store, old.memory_id, replacement, recorder=operations) == "new"
    assert old.superseded_by == replacement.memory_id
    assert old.quarantined
    assert "quarantine:superseded" in old.tags
    assert replacement.links == ["supporting-observation", "old"]
    assert operations[0]["target_id"] == "new"

    # Retrying the same mutation neither duplicates the provenance link nor emits
    # a second state transition for an already quarantined predecessor.
    assert supersede(store, old.memory_id, replacement, recorder=operations) == "new"
    assert replacement.links.count("old") == 1
    assert len(operations) == 1


def test_bm25_backed_store_short_circuits_when_disabled_or_limit_nonpositive(monkeypatch):
    def unexpected_index_read(*_args, **_kwargs):
        raise AssertionError("lexical index must not be queried by an abstaining store")

    monkeypatch.setattr(
        "core.memory.segmented_bm25.SegmentedBM25Index.rank_with_scores",
        unexpected_index_read,
    )

    disabled = TieredMemoryStore(enabled=False)
    disabled.add(_record("disabled-record", tags=["carrier"]))
    assert disabled.retrieve(["carrier"], ["r230"]) == _EMPTY_RETRIEVAL

    enabled = TieredMemoryStore()
    enabled.add(_record("limited-record", tags=["carrier"]))
    assert enabled.retrieve(["carrier"], ["r230"], limit_per_tier=-1) == _EMPTY_RETRIEVAL


def test_bm25_core_returns_empty_for_empty_index_and_nonpositive_limit():
    assert BM25Index({}).rank("carrier", 3) == []
    index = BM25Index({"carrier": tokenize("carrier interface down")})
    assert index.rank("carrier", 0) == []
    assert index.rank_with_scores("carrier", -1) == []


def test_exact_service_entity_recalls_chinese_text_and_is_visible_in_diagnostics():
    store = TieredMemoryStore()
    matching = MemoryRecord(
        memory_id="matching-unit",
        tier="episodic",
        text="哨兵发现 demo-collector.service 连续启动失败",
    )
    near_miss = MemoryRecord(
        memory_id="near-miss-unit",
        tier="episodic",
        text="哨兵发现 demo-collector.service-old 连续启动失败",
    )
    store.seed([matching, near_miss])

    result = store.retrieve(["demo-collector.service"], [], limit_per_tier=5)
    assert result["episodic"] == [matching]

    diagnostics = store.retrieval_diagnostics()
    assert len(diagnostics) == 1
    detail = diagnostics[0]
    assert detail["entity_hits"] == ["demo-collector.service"]
    assert detail["entity_score"] > 0.0
    assert {
        "memory_id",
        "tier",
        "lexical_score",
        "asset_hits",
        "vector_score",
        "graph_hop",
        "graph_parent_id",
        "structural_prior",
        "final_score",
    }.issubset(detail)


@pytest.mark.parametrize(
    ("entity", "record_field"),
    [
        ("192.168.1.27", "text"),
        ("10.24.0.0/16", "tags"),
        ("eno2", "asset_ids"),
        ("r230", "text"),
        ("NXDOMAIN", "text"),
    ],
)
def test_exact_entity_shapes_match_text_tags_and_assets(entity, record_field):
    kwargs = {"text": "没有相关内容", "tags": [], "asset_ids": []}
    if record_field == "text":
        kwargs["text"] = f"实体 {entity}. 已被观测"
    else:
        kwargs[record_field] = [entity]
    record = MemoryRecord(memory_id=f"entity-{record_field}", tier="semantic", **kwargs)
    store = TieredMemoryStore()
    store.add(record)

    assert store.retrieve([entity], [], limit_per_tier=2)["semantic"] == [record]


def test_non_entity_query_keeps_existing_lexical_ranking():
    store = TieredMemoryStore()
    first = _record("first", tags=["carrier"])
    second = _record("second", tags=["carrier"])
    store.seed([first, second])

    assert store.retrieve(["carrier"], [], limit_per_tier=5)["episodic"] == [
        first,
        second,
    ]
    assert all(
        detail["entity_hits"] == [] and detail["entity_score"] == 0.0
        for detail in store.retrieval_diagnostics()
    )

from __future__ import annotations

from datetime import datetime, timezone

from core.investigate.evidence_retrieval import (
    EntityRelation,
    EvidenceCandidate,
    EvidenceRetriever,
    RetrievalScope,
)


def _time(hour: int, day: int = 20) -> datetime:
    return datetime(2026, 8, day, hour, tzinfo=timezone.utc)


def _scope(**overrides) -> RetrievalScope:
    values = {
        "query_text": "firewall deny camera service policy",
        "asset_ids": ("camera-01",),
        "incident_start": _time(10),
        "incident_end": _time(11),
        "allowed_sources": ("clickhouse", "fortios-docs", "memory"),
        "device_versions": ("7.4.6",),
        "asset_versions": {"camera-01": "7.4.6"},
        "seed_entities": ("ip:192.168.1.20",),
    }
    values.update(overrides)
    return RetrievalScope(**values)


class _RecordingDense:
    def __init__(self, ranking: list[str]):
        self.ranking = ranking
        self.seen_ids: list[str] = []

    def rank(self, query_text, candidates, k):
        self.seen_ids = [item.evidence_id for item in candidates]
        return [(evidence_id, 1.0) for evidence_id in self.ranking[:k]]


def test_metadata_prefilter_runs_before_bm25_and_dense_ranking():
    candidates = [
        EvidenceCandidate(
            "fact-current",
            "current flow was denied by policy 18",
            "current_fact",
            "clickhouse",
            asset_ids=("camera-01",),
            observed_at=_time(10),
        ),
        EvidenceCandidate(
            "doc-eligible",
            "camera service can be denied by a firewall policy",
            "document",
            "fortios-docs",
            asset_ids=("camera-01",),
            applicable_versions=("7.4.6",),
        ),
        EvidenceCandidate(
            "doc-wrong-device",
            "firewall deny camera service policy",
            "document",
            "fortios-docs",
            asset_ids=("camera-02",),
            applicable_versions=("7.4.6",),
        ),
        EvidenceCandidate(
            "doc-old-version",
            "firewall deny camera service policy",
            "document",
            "fortios-docs",
            asset_ids=("camera-01",),
            applicable_versions=("6.4.0",),
        ),
        EvidenceCandidate(
            "doc-untrusted-source",
            "firewall deny camera service policy",
            "document",
            "random-wiki",
            asset_ids=("camera-01",),
            applicable_versions=("7.4.6",),
        ),
        EvidenceCandidate(
            "fact-outside-window",
            "firewall deny camera service policy",
            "current_fact",
            "clickhouse",
            asset_ids=("camera-01",),
            observed_at=_time(8),
        ),
    ]
    dense = _RecordingDense(
        ["doc-wrong-device", "doc-old-version", "doc-eligible", "unknown"]
    )

    result = EvidenceRetriever(candidates, dense_ranker=dense).retrieve(_scope())

    assert dense.seen_ids == ["doc-eligible"]
    assert [item.candidate.evidence_id for item in result.kept] == [
        "fact-current",
        "doc-eligible",
    ]
    dropped = {item.candidate.evidence_id: item.reasons for item in result.dropped}
    assert "asset_mismatch" in dropped["doc-wrong-device"]
    assert "version_mismatch" in dropped["doc-old-version"]
    assert "source_not_allowed" in dropped["doc-untrusted-source"]
    assert "outside_incident_window" in dropped["fact-outside-window"]


def test_ip_reassignment_cannot_override_asset_identity():
    shared_ip = "ip:192.168.1.20"
    candidates = [
        EvidenceCandidate(
            "new-owner-fact",
            "camera service denied now",
            "current_fact",
            "clickhouse",
            asset_ids=("camera-01",),
            entity_ids=(shared_ip,),
            observed_at=_time(10),
        ),
        EvidenceCandidate(
            "old-owner-incident",
            "firewall deny on the same address last week",
            "historical_incident",
            "memory",
            asset_ids=("retired-printer",),
            entity_ids=(shared_ip,),
            observed_at=_time(10, day=13),
        ),
    ]
    relations = [EntityRelation("asset:camera-01", shared_ip)]

    result = EvidenceRetriever(candidates, relations=relations).retrieve(_scope())

    assert [item.candidate.evidence_id for item in result.kept] == ["new-owner-fact"]
    old = next(
        item for item in result.dropped if item.candidate.evidence_id == "old-owner-incident"
    )
    assert "asset_mismatch" in old.reasons


def test_structured_current_fact_cannot_be_displaced_by_top_n_text_matches():
    candidates = [
        EvidenceCandidate(
            "fact",
            "policy_id=18 action=deny bytes=0",
            "current_fact",
            "clickhouse",
            asset_ids=("camera-01",),
            observed_at=_time(10),
        ),
        *[
            EvidenceCandidate(
                f"doc-{index}",
                "firewall deny camera service policy " * (5 - index),
                "document",
                "fortios-docs",
                applicable_versions=("7.4.6",),
            )
            for index in range(4)
        ],
    ]

    result = EvidenceRetriever(candidates).retrieve(_scope(), top_k=2)

    assert result.kept[0].candidate.evidence_id == "fact"
    assert result.kept[0].reasons == ("structured_fact_priority",)
    assert len(result.kept) == 2
    assert all(item.candidate.kind != "current_fact" for item in result.kept[1:])
    assert sum(
        "top_k_limit" in item.reasons for item in result.dropped
    ) == 3


def test_context_pool_retains_supporting_and_opposing_evidence():
    candidates = [
        EvidenceCandidate(
            "support",
            "firewall deny camera service policy policy policy",
            "document",
            "fortios-docs",
            polarity="supports",
            hypothesis_ids=("h-firewall",),
        ),
        EvidenceCandidate(
            "distractor",
            "firewall deny camera service policy service",
            "document",
            "fortios-docs",
        ),
        EvidenceCandidate(
            "oppose",
            "policy counter is zero",
            "current_fact",
            "clickhouse",
            asset_ids=("camera-01",),
            observed_at=_time(10),
            polarity="opposes",
            hypothesis_ids=("h-firewall",),
        ),
    ]

    result = EvidenceRetriever(candidates).retrieve(
        _scope(target_hypothesis_ids=("h-firewall",)), top_k=2
    )

    assert {item.candidate.evidence_id for item in result.kept} == {"support", "oppose"}
    assert {item.candidate.polarity for item in result.kept} == {"supports", "opposes"}
    assert result.kept[0].candidate.evidence_id == "oppose"


def test_missing_dense_ranker_degrades_to_deterministic_bm25():
    candidates = [
        EvidenceCandidate(
            "lexical",
            "firewall deny camera service policy",
            "document",
            "fortios-docs",
        ),
        EvidenceCandidate(
            "unrelated",
            "administrator authentication lockout",
            "document",
            "fortios-docs",
        ),
    ]
    retriever = EvidenceRetriever(candidates)

    first = retriever.retrieve(_scope(), top_k=2)
    second = retriever.retrieve(_scope(), top_k=2)

    assert first.dense_used is False
    assert first == second
    assert [item.candidate.evidence_id for item in first.kept] == ["lexical"]
    assert next(item for item in first.dropped).reasons == ("no_retrieval_signal",)


def test_upstream_hybrid_rank_survives_context_prefilter_and_fusion():
    candidates = [
        EvidenceCandidate(
            "semantic-hit",
            "link flapping after firmware transition",
            "historical_incident",
            "memory",
            asset_ids=("camera-01",),
            upstream_rank=1,
        ),
        EvidenceCandidate(
            "lexical-hit",
            "firewall deny camera service policy",
            "historical_incident",
            "memory",
            asset_ids=("camera-01",),
            upstream_rank=2,
        ),
    ]

    result = EvidenceRetriever(candidates).retrieve(_scope(), top_k=2)

    assert {item.candidate.evidence_id for item in result.kept} == {
        "semantic-hit", "lexical-hit",
    }
    semantic = next(
        item for item in result.kept if item.candidate.evidence_id == "semantic-hit"
    )
    assert "upstream_rank:1" in semantic.reasons


def test_historical_incident_is_compiled_as_reference_even_if_claimed_decisive():
    candidates = [
        EvidenceCandidate(
            "fact",
            "camera service policy denied current flow",
            "current_fact",
            "clickhouse",
            asset_ids=("camera-01",),
            observed_at=_time(10),
            claimed_decisive=True,
        ),
        EvidenceCandidate(
            "old-case",
            "camera service policy denied historical flow",
            "historical_incident",
            "memory",
            asset_ids=("camera-01",),
            observed_at=_time(9, day=13),
            claimed_decisive=True,
        ),
        EvidenceCandidate(
            "hypothesis",
            "firewall policy may deny the service",
            "hypothesis",
            "memory",
            asset_ids=("camera-01",),
        ),
    ]

    result = EvidenceRetriever(candidates).retrieve(_scope())
    sections = result.context_sections()

    assert sections["current_fact"][0].decisive_for_current_incident is True
    old_case = sections["historical_incident"][0]
    assert old_case.decisive_for_current_incident is False
    assert "historical_incident_is_reference_only" in old_case.reasons
    assert sections["hypothesis"][0].decisive_for_current_incident is False


def test_entity_expansion_is_time_bounded_and_stops_at_configured_hops():
    relations = [
        EntityRelation("asset:camera-01", "interface:lan1"),
        EntityRelation("interface:lan1", "service:camera"),
        EntityRelation("service:camera", "host:recorder"),
        EntityRelation(
            "asset:camera-01",
            "service:expired",
            valid_to=_time(9),
        ),
    ]
    candidates = [
        EvidenceCandidate(
            "two-hops",
            "camera service firewall deny precedent",
            "historical_incident",
            "memory",
            entity_ids=("service:camera",),
            observed_at=_time(8, day=13),
        ),
        EvidenceCandidate(
            "three-hops",
            "camera service firewall deny recorder precedent",
            "historical_incident",
            "memory",
            entity_ids=("host:recorder",),
            observed_at=_time(8, day=13),
        ),
        EvidenceCandidate(
            "expired-edge",
            "camera service firewall deny expired relation",
            "historical_incident",
            "memory",
            entity_ids=("service:expired",),
            observed_at=_time(8, day=13),
        ),
    ]

    result = EvidenceRetriever(candidates, relations=relations).retrieve(
        _scope(seed_entities=(), max_relation_hops=2)
    )

    assert "service:camera" in result.expanded_entities
    assert "host:recorder" not in result.expanded_entities
    assert [item.candidate.evidence_id for item in result.kept] == ["two-hops"]
    dropped = {item.candidate.evidence_id: item.reasons for item in result.dropped}
    assert "entity_unreachable" in dropped["three-hops"]
    assert "entity_unreachable" in dropped["expired-edge"]

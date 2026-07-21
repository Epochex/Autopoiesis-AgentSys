import json
import random
from concurrent.futures import ThreadPoolExecutor

import pytest

import core.memory.segmented_bm25 as segmented_bm25
from core.memory.bm25 import BM25Index, tokenize
from core.memory.segmented_bm25 import SegmentedBM25Index, SnapshotCorruptionError


def _assert_equivalent(index, documents, queries):
    baseline = BM25Index(documents, k1=index.k1, b=index.b)
    for query in queries:
        assert index.rank_with_scores(query, 100) == baseline.rank_with_scores(query, 100)


def test_segmented_scores_equal_monolithic_bm25_across_seals():
    documents = {
        "a": tokenize("carrier interface packet loss"),
        "b": tokenize("database connection timeout retry retry"),
        "c": tokenize("carrier route timeout"),
        "d": tokenize("payment price approval"),
        "e": [],
    }
    index = SegmentedBM25Index(seal_threshold=2, compact_segment_threshold=10)
    for offset, (doc_id, tokens) in enumerate(documents.items(), start=10):
        index.upsert(doc_id, tokens, offset=offset)
    _assert_equivalent(
        index,
        documents,
        ["carrier timeout", "retry retry", "approval price", "not present", ""],
    )
    assert index.health()["segment_count"] == 2
    assert index.health()["delta_entries"] == 1
    assert index.rank_with_scores(["carrier", "timeout"]) == BM25Index(
        documents
    ).rank_with_scores("", len(documents), query_tokens=["carrier", "timeout"])


def test_maybe_seal_and_maybe_compact_respect_thresholds():
    index = SegmentedBM25Index(seal_threshold=3, compact_segment_threshold=3)
    index.upsert("a", ["alpha"], 1)
    index.upsert("b", ["beta"], 2)
    assert not index.maybe_seal()
    index.seal()
    index.upsert("c", ["gamma"], 3)
    index.seal()
    assert not index.maybe_compact()


def test_obsolete_ratio_can_trigger_compaction_below_segment_threshold():
    index = SegmentedBM25Index(
        seal_threshold=1,
        compact_segment_threshold=99,
        obsolete_ratio_threshold=0.50,
        min_compaction_entries=4,
    )
    index.upsert("a", ["old"], 1)
    index.upsert("a", ["current"], 2)
    index.upsert("b", ["temporary"], 3)
    assert not index.health()["compaction_due"]  # only 3 physical entries
    index.delete("b", 4)
    assert index.health()["obsolete_ratio"] == pytest.approx(0.75)
    assert index.health()["compaction_due"]
    assert index.maybe_compact()
    assert index.health()["physical_entries"] == 1
    assert index.health()["obsolete_entries"] == 0
    assert index.rank("current", 10) == ["a"]


def test_upsert_and_delete_filter_old_versions_and_tombstones():
    index = SegmentedBM25Index(seal_threshold=1, compact_segment_threshold=20)
    index.upsert("same", tokenize("old carrier fault"), offset=1)
    index.upsert("keep", tokenize("stable carrier route"), offset=2)
    index.upsert("same", tokenize("new payment approval"), offset=3)
    assert index.rank("old fault", 10) == []
    assert index.rank("new approval", 10) == ["same"]
    assert index.delete("same", offset=4)
    assert not index.delete("unknown", offset=5)
    assert index.rank("new approval", 10) == []
    assert index.rank("carrier", 10) == ["keep"]
    health = index.health()
    assert health["live_documents"] == 1
    assert health["physical_entries"] == 5
    assert health["obsolete_entries"] == 4


def test_compaction_requires_threshold_then_removes_obsolete_entries():
    index = SegmentedBM25Index(seal_threshold=2, compact_segment_threshold=3)
    index.upsert("a", ["old"], offset=1)
    index.upsert("b", ["keep"], offset=2)
    index.upsert("a", ["new"], offset=3)
    index.seal()
    before = index.health()
    assert before["segment_count"] == 2
    assert before["obsolete_entries"] == 1
    assert not index.compact()
    assert index.health() == before

    index.upsert("c", ["third"], offset=4)
    index.seal()
    assert index.health()["compaction_due"]
    assert index.maybe_compact()
    after = index.health()
    assert after["segment_count"] == 1
    assert after["physical_entries"] == 3
    assert after["obsolete_entries"] == 0
    assert index.rank("old", 10) == []
    assert index.rank("new", 10) == ["a"]


def test_force_compaction_also_merges_delta_and_purges_tombstones():
    index = SegmentedBM25Index(seal_threshold=10, compact_segment_threshold=4)
    index.upsert("a", ["one"], offset=1)
    index.upsert("b", ["two"], offset=2)
    index.delete("a", offset=3)
    assert index.compact(force=True)
    assert index.health()["delta_entries"] == 0
    assert index.health()["physical_entries"] == 1
    assert index.rank("one", 10) == []


def test_snapshot_round_trip_preserves_generation_offset_results_and_can_continue(tmp_path):
    path = tmp_path / "nested" / "index.json"
    index = SegmentedBM25Index(
        seal_threshold=2,
        compact_segment_threshold=9,
        obsolete_ratio_threshold=0.35,
        min_compaction_entries=17,
    )
    index.upsert("a", tokenize("alpha carrier"), offset=101)
    index.upsert("b", tokenize("beta database"), offset=102)
    index.upsert("a", tokenize("alpha payment"), offset=103)
    digest = index.save(path)

    restored = SegmentedBM25Index.load(path)
    assert len(digest) == 64
    assert restored.generation == index.generation
    assert restored.applied_offset == 103
    assert restored.obsolete_ratio_threshold == 0.35
    assert restored.min_compaction_entries == 17
    assert restored.health() == index.health()
    assert restored.rank_with_scores("alpha payment database", 10) == index.rank_with_scores(
        "alpha payment database", 10
    )
    restored.upsert("c", ["payment"], offset=104)
    assert restored.applied_offset == 104
    with pytest.raises(ValueError, match="not newer"):
        restored.delete("c", offset=104)


def test_checksum_rejects_tampering_and_invalid_json(tmp_path):
    path = tmp_path / "index.json"
    index = SegmentedBM25Index()
    index.upsert("a", ["alpha"], offset=1)
    index.save(path)
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["state"]["delta"][0]["tokens"] = ["tampered"]
    path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(SnapshotCorruptionError, match="checksum mismatch"):
        SegmentedBM25Index.load(path)

    path.write_text("not-json", encoding="utf-8")
    with pytest.raises(SnapshotCorruptionError, match="cannot read"):
        SegmentedBM25Index.load(path)


def test_failed_atomic_replace_preserves_previous_snapshot_and_cleans_temp(tmp_path, monkeypatch):
    path = tmp_path / "index.json"
    index = SegmentedBM25Index()
    index.upsert("a", ["before"], 1)
    index.save(path)
    previous = path.read_bytes()
    index.upsert("b", ["after"], 2)

    def fail_replace(source, destination):
        raise OSError("injected replace failure")

    monkeypatch.setattr(segmented_bm25.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected"):
        index.save(path)
    assert path.read_bytes() == previous
    assert list(tmp_path.glob(".index.json.*")) == []


def test_random_mutations_remain_equivalent_to_fresh_bm25():
    rng = random.Random(7)
    vocabulary = ["alpha", "beta", "carrier", "route", "price", "database"]
    live = {}
    index = SegmentedBM25Index(seal_threshold=7, compact_segment_threshold=4)
    for offset in range(1, 151):
        doc_id = f"doc-{rng.randrange(25):02d}"
        if rng.random() < 0.22:
            index.delete(doc_id, offset=offset)
            live.pop(doc_id, None)
        else:
            tokens = [rng.choice(vocabulary) for _ in range(rng.randrange(1, 8))]
            index.upsert(doc_id, tokens, offset=offset)
            live[doc_id] = tokens
        if offset % 11 == 0:
            _assert_equivalent(index, live, vocabulary + ["alpha price", "route route"])
    _assert_equivalent(index, live, vocabulary + ["alpha price", "route route"])


def test_concurrent_readers_and_writer_observe_only_valid_rankings():
    index = SegmentedBM25Index(seal_threshold=8, compact_segment_threshold=5)
    for number in range(20):
        index.upsert(f"doc-{number}", ["common", f"value{number}"], offset=number)

    failures = []

    def reader():
        for _ in range(250):
            result = index.rank_with_scores("common", 50)
            ids = [doc_id for doc_id, score in result]
            if len(ids) != len(set(ids)) or any(score <= 0 for _, score in result):
                failures.append(result)

    def writer():
        for number in range(20, 120):
            index.upsert(f"doc-{number % 35}", ["common", f"value{number}"])
            if number % 9 == 0:
                index.delete(f"doc-{(number + 3) % 35}")

    with ThreadPoolExecutor(max_workers=5) as pool:
        jobs = [pool.submit(reader) for _ in range(4)] + [pool.submit(writer)]
        for job in jobs:
            job.result()
    assert failures == []
    health = index.health()
    assert 0 <= health["obsolete_ratio"] <= 1
    assert health["live_documents"] <= 35

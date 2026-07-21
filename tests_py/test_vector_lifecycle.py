from __future__ import annotations

import json

import pytest


np = pytest.importorskip("numpy")
pytest.importorskip("faiss")

from core.memory.vector_lifecycle import IndexSnapshotError, VectorIndexLifecycle


def vectors(*rows):
    return np.asarray(rows, dtype="float32")


def test_upsert_update_delete_and_merged_ranking():
    index = VectorIndexLifecycle.build(
        ["old", "other"],
        vectors([1, 0], [0, 1]),
        base_index_type="flat",
        applied_offset=10,
        delta_ratio_threshold=1.0,
    )

    assert index.search([1, 0], 2) == ["old", "other"]
    assert index.upsert("new", [0.8, 0.2], offset=11)
    assert index.upsert("old", [0, -1], offset=12)
    assert index.search([1, 0], 3) == ["new", "old", "other"]

    assert index.delete("new", offset=13)
    assert index.search([1, 0], 3) == ["old", "other"]
    assert index.stats.tombstones == 1
    assert index.stats.obsolete_vectors == 2
    assert index.delete("new", offset=13) is False  # replayed source event


def test_versions_reject_stale_updates_and_delete_is_immediately_visible():
    index = VectorIndexLifecycle.build(["a"], vectors([1, 0]), base_index_type="flat")
    assert index.delete("a", offset=1, version=2)
    assert index.search([1, 0], 10) == []
    with pytest.raises(ValueError, match="version must be at least 3"):
        index.upsert("a", [1, 0], offset=2, version=2)
    assert index.upsert("a", [0, 1], offset=2, version=3)
    assert index.search_hits([0, 1], 1)[0].version == 3
    with pytest.raises(TypeError, match="offset"):
        index.delete("a", offset=True)
    with pytest.raises(TypeError, match="version"):
        index.delete("a", offset=3, version=True)


def test_compaction_threshold_and_rebuild_remove_obsolete_vectors():
    index = VectorIndexLifecycle.build(
        ["a", "b", "c", "d"],
        vectors([1, 0], [0, 1], [-1, 0], [0, -1]),
        base_index_type="hnsw",
        delta_ratio_threshold=0.50,
        obsolete_ratio_threshold=0.20,
    )
    assert not index.should_compact()
    index.upsert("a", [0.7, 0.7], offset=1)
    assert index.should_compact()  # one stale vector out of five exceeds 20%
    before = index.search([1, 1], 4)
    old_generation = index.generation

    assert index.compact() == old_generation + 1
    assert index.stats.delta_vectors == 0
    assert index.stats.obsolete_vectors == 0
    assert index.stats.base_vectors == 4
    assert index.stats.tombstones == 0
    assert index.search([1, 1], 4) == before
    assert index.search([1, 1], 1)[0] == "a"


def test_persist_restart_retains_delta_tombstones_and_offset(tmp_path):
    index = VectorIndexLifecycle.build(
        ["a", "b"], vectors([1, 0], [0, 1]), base_index_type="hnsw", applied_offset=7
    )
    index.upsert("c", [0.9, 0.1], offset=8)
    index.delete("a", offset=9)
    snapshot = index.save(tmp_path)

    assert snapshot.is_dir()
    restored = VectorIndexLifecycle.load(tmp_path)
    assert restored.generation == index.generation
    assert restored.applied_offset == 9
    assert restored.search([1, 0], 3) == ["c", "b"]
    assert restored.upsert("ignored", [1, 0], offset=9) is False
    assert restored.upsert("fresh", [1, 0], offset=10)


def test_restart_without_delta_does_not_require_rebuild(tmp_path):
    index = VectorIndexLifecycle.build(["a", "b"], vectors([1, 0], [0, 1]), base_index_type="hnsw")
    index.save(tmp_path)

    restored = VectorIndexLifecycle.load(tmp_path)
    assert restored.search([1, 0], 2) == ["a", "b"]
    assert restored.health()["delta"] == 0


def test_health_exposes_capacity_visibility_and_checkpoint():
    index = VectorIndexLifecycle.build(["a", "b"], vectors([1, 0], [0, 1]), base_index_type="flat")
    index.upsert("a", [0.7, 0.7], offset=1)
    index.delete("b", offset=2)

    health = index.health()
    assert health == {
        "healthy": True,
        "generation": 1,
        "applied_offset": 2,
        "physical_vectors": 3,
        "live": 1,
        "tombstones": 1,
        "base": 2,
        "delta": 1,
        "obsolete": 2,
        "obsolete_ratio": pytest.approx(2 / 3),
        "compaction_due": True,
    }


def test_corrupt_payload_is_rejected_before_faiss_load(tmp_path):
    index = VectorIndexLifecycle.build(["a"], vectors([1, 0]), base_index_type="flat")
    snapshot = index.save(tmp_path)
    with (snapshot / "metadata.json").open("a", encoding="utf-8") as handle:
        handle.write("corruption")

    with pytest.raises(IndexSnapshotError, match="checksum"):
        VectorIndexLifecycle.load(tmp_path)


def test_manifest_generation_mismatch_is_rejected(tmp_path):
    index = VectorIndexLifecycle.build(["a"], vectors([1, 0]), base_index_type="flat")
    snapshot = index.save(tmp_path)
    metadata_path = snapshot / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["generation"] += 1
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    # Re-signing one file simulates a structurally valid but semantically
    # inconsistent snapshot, which must still fail validation.
    manifest_path = snapshot / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    import hashlib

    payload = metadata_path.read_bytes()
    manifest["files"]["metadata.json"] = {
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(IndexSnapshotError, match="generation differs"):
        VectorIndexLifecycle.load(tmp_path)


def test_snapshot_retention_removes_only_old_complete_generations(tmp_path):
    index = VectorIndexLifecycle.build(["a"], vectors([1, 0]), base_index_type="flat")
    first = index.save(tmp_path, keep_snapshots=2)
    index.upsert("b", [0, 1], offset=1)
    second = index.save(tmp_path, keep_snapshots=2)
    index.upsert("c", [-1, 0], offset=2)
    third = index.save(tmp_path, keep_snapshots=2)

    assert not first.exists()
    assert second.exists() and third.exists()
    assert (tmp_path / "CURRENT").read_text(encoding="utf-8").strip() == third.name

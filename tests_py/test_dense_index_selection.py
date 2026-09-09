import pytest

from core.eval.dense_retrieval import select_hnsw_ef_search, select_index_type


def test_auto_index_stays_exact_below_measured_boundary():
    assert select_index_type(9_014, flat_max_documents=100_000) == "flat"


def test_auto_index_moves_large_corpus_to_hnsw():
    assert select_index_type(1_000_000, flat_max_documents=100_000) == "hnsw"


def test_explicit_index_override_wins():
    assert select_index_type(1_000_000, "flat") == "flat"
    with pytest.raises(ValueError):
        select_index_type(10, "unknown")


def test_hnsw_search_breadth_uses_measured_scale_and_recall_target():
    assert select_hnsw_ef_search(100_000, target_recall=0.95) == 512
    assert select_hnsw_ef_search(1_000_000, target_recall=0.95) == 2304
    assert select_hnsw_ef_search(1_000_000, target_recall=0.99) == 4608

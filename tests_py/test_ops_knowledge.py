from core.memory.ops_knowledge import retrieve_ops_knowledge


def test_failed_service_query_returns_scored_systemd_passages():
    rows = retrieve_ops_knowledge(
        "demo-collector.service failed",
        query_terms=["service", "failed", "systemctl"],
        limit=3,
    )

    assert rows
    assert rows[0]["document_id"] == "systemctl-failed-units"
    assert rows[0]["route"] == "bm25"
    assert rows[0]["score"] > 0
    assert "service" in rows[0]["matched_terms"]
    assert all(row["source"] and row["locator"] and row["text"] for row in rows)


def test_unknown_query_abstains_instead_of_returning_random_documents():
    assert retrieve_ops_knowledge("xyzzy quantum teapot", limit=4) == []

import json

from core.memory.ops_knowledge import knowledge_backend_status, retrieve_ops_knowledge


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


def test_live_retrieval_uses_configured_external_corpus(monkeypatch, tmp_path):
    automatic_dir = tmp_path / "automatic"
    monkeypatch.setenv("AUTOPOIESIS_KNOWLEDGE_DIR", str(automatic_dir))
    corpus = tmp_path / "knowledge.json"
    corpus.write_text(json.dumps([
        {
            "id": "route-change-runbook",
            "text": "Compare current route state with the configuration revision after a change.",
            "title": "Route change investigation",
            "source": "approved-runbook",
            "locator": "runbook:route-change",
            "applicable_versions": ["7.4.6"],
        }
    ]), encoding="utf-8")
    monkeypatch.setenv("AUTOPOIESIS_KNOWLEDGE_CORPUS_PATH", str(corpus))
    monkeypatch.setenv("AUTOPOIESIS_KNOWLEDGE_RETRIEVAL_MODE", "bm25")

    rows = retrieve_ops_knowledge("route configuration revision", limit=2)

    assert [row["document_id"] for row in rows] == ["route-change-runbook"]
    assert rows[0]["source"] == "approved-runbook"
    assert rows[0]["applicable_versions"] == ["7.4.6"]
    assert knowledge_backend_status() == {
        "corpus": str(corpus.resolve()),
        "corpus_source": "configured_file",
        "external_configured": True,
        "document_count": 1,
        "route": "bm25",
        "dense_enabled": False,
        "degraded": False,
        "degraded_reason": None,
        "automatic_sync": {
            "state": "never_checked",
            "corpusPath": str(automatic_dir / "current-corpus.json"),
        },
    }

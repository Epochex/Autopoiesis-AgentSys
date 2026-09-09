from __future__ import annotations

from datetime import datetime, timezone
import json

from core.memory.knowledge_sync import DeviceFingerprint, synchronize_official_knowledge


class _Source:
    source_id = "test-official-source"

    def supports(self, fingerprint):
        return fingerprint.vendor == "Fortinet"

    def build(self, fingerprint):
        return {
            "source": "official-test",
            "source_base_url": "https://docs.example.invalid/guide",
            "coverage": 1.0,
            "chunks": [{
                "id": "guide#1",
                "text": "Route and policy troubleshooting.",
                "product": fingerprint.product,
                "applicable_versions": [fingerprint.release],
            }],
        }


def test_device_fingerprint_publishes_corpus_without_manual_path(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOPOIESIS_KNOWLEDGE_DIR", str(tmp_path))
    fingerprint = DeviceFingerprint("Fortinet", "FortiGate", "FG100E", "v6.2.7", "serial")

    result = synchronize_official_knowledge(
        fingerprint=fingerprint,
        sources=[_Source()],
        now=lambda: datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc),
    )

    assert result["state"] == "ready"
    assert result["documentCount"] == 1
    assert (tmp_path / "current-corpus.json").is_file()
    assert (tmp_path / "status.json").is_file()


def test_same_fingerprint_skips_recent_refresh(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOPOIESIS_KNOWLEDGE_DIR", str(tmp_path))
    fingerprint = DeviceFingerprint("Fortinet", "FortiGate", "FG100E", "v6.2.7", "serial")
    clock = lambda: datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    synchronize_official_knowledge(
        fingerprint=fingerprint, sources=[_Source()], now=clock,
    )

    result = synchronize_official_knowledge(
        fingerprint=fingerprint, sources=[_Source()], now=clock,
    )

    assert result["state"] == "ready"
    assert result["refreshSkipped"] is True
    persisted = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert persisted == result
    assert persisted["lastSkipAt"] == "2026-09-02T12:00:00+00:00"


def test_recent_corpus_repairs_stale_synchronizing_receipt(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOPOIESIS_KNOWLEDGE_DIR", str(tmp_path))
    fingerprint = DeviceFingerprint("Fortinet", "FortiGate", "FG100E", "v6.2.7", "serial")
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    (tmp_path / "current-corpus.json").write_text(
        json.dumps({"chunks": [{"id": "existing", "text": "existing corpus"}]}),
        encoding="utf-8",
    )
    (tmp_path / "status.json").write_text(
        json.dumps({
            "state": "synchronizing",
            "checkedAt": now.isoformat(),
            "fingerprintKey": fingerprint.key,
            "servingPreviousGeneration": True,
        }),
        encoding="utf-8",
    )

    result = synchronize_official_knowledge(
        fingerprint=fingerprint,
        sources=[_Source()],
        now=lambda: now,
    )

    assert result["state"] == "ready"
    assert result["refreshSkipped"] is True
    assert result["servingPreviousGeneration"] is False
    assert result["documentCount"] == 1
    assert result["publishedAt"]
    assert json.loads((tmp_path / "status.json").read_text(encoding="utf-8")) == result

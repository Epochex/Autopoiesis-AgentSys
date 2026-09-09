"""Device-driven synchronization of official operations documentation.

The investigation request path only reads a completed corpus generation.  This
module discovers the managed appliance identity, selects an approved vendor
source, downloads documentation in the background, validates coverage, and
atomically publishes one JSON corpus.  A failed refresh leaves the previous
generation serving.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.request import Request, urlopen


@dataclass(frozen=True, slots=True)
class DeviceFingerprint:
    vendor: str
    product: str
    model: str
    version: str
    serial: str

    @property
    def release(self) -> str:
        match = re.search(r"(\d+)\.(\d+)", self.version)
        return f"{match.group(1)}.{match.group(2)}" if match else ""

    @property
    def key(self) -> str:
        raw = "|".join((self.vendor, self.product, self.model, self.version, self.serial))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


class KnowledgeSource(Protocol):
    source_id: str

    def supports(self, fingerprint: DeviceFingerprint) -> bool: ...

    def build(self, fingerprint: DeviceFingerprint) -> dict[str, Any]: ...


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _fetch_text(url: str, timeout: float = 30.0) -> str:
    request = Request(url, headers={"User-Agent": "autopoiesis-knowledge-sync/1.0"})
    with urlopen(request, timeout=timeout) as response:
        raw = response.read()
    return raw.decode("utf-8", errors="replace")


class FortinetAdministrationGuideSource:
    """Approved source adapter for the installed FortiGate release family."""

    source_id = "fortinet-fortios-administration-guide"
    _SEED = "954635/getting-started"

    def __init__(
        self,
        *,
        fetcher: Callable[[str], str] = _fetch_text,
        workers: int = 4,
        minimum_coverage: float = 0.80,
    ) -> None:
        self._fetcher = fetcher
        self._workers = max(1, min(int(workers), 12))
        self._minimum_coverage = max(0.1, min(float(minimum_coverage), 1.0))

    def supports(self, fingerprint: DeviceFingerprint) -> bool:
        return (
            fingerprint.vendor.casefold() == "fortinet"
            and fingerprint.product.casefold() == "fortigate"
            and bool(fingerprint.release)
        )

    def _release_candidates(self, fingerprint: DeviceFingerprint) -> list[str]:
        numbers = re.findall(r"\d+", fingerprint.version)
        values: list[str] = []
        if len(numbers) >= 3:
            values.append(".".join(numbers[:3]))
        if fingerprint.release:
            values.append(f"{fingerprint.release}.0")
        return list(dict.fromkeys(values))

    def build(self, fingerprint: DeviceFingerprint) -> dict[str, Any]:
        from core.eval.fortios_corpus import chunk_section, extract_blocks, parse_toc

        guide_base = ""
        seed_html = ""
        failures: list[str] = []
        for release in self._release_candidates(fingerprint):
            candidate = f"https://docs.fortinet.com/document/fortigate/{release}/administration-guide"
            try:
                html = self._fetcher(f"{candidate}/{self._SEED}")
                if parse_toc(html):
                    guide_base, seed_html = candidate, html
                    break
            except Exception as error:  # source failures are recorded in status
                failures.append(f"{release}:{type(error).__name__}")
        if not guide_base:
            raise RuntimeError(
                "no compatible Fortinet Administration Guide found for "
                f"{fingerprint.version}; attempts={','.join(failures) or 'none'}"
            )

        toc = parse_toc(seed_html)
        installed_version = fingerprint.version.strip()
        plain_version = installed_version.removeprefix("v")

        def parse_page(
            item: tuple[str, tuple[str, str, list[str]]],
        ) -> tuple[str, list[dict[str, Any]] | None]:
            section_id, (title, slug, ancestors) = item
            try:
                html = (
                    seed_html
                    if section_id == "954635"
                    else self._fetcher(f"{guide_base}/{section_id}/{slug}")
                )
            except Exception:
                return section_id, None
            if 'id="mc-main-content"' not in html:
                return section_id, None
            rows: list[dict[str, Any]] = []
            for chunk in chunk_section(section_id, title, ancestors, extract_blocks(html)):
                row = asdict(chunk)
                row.update({
                    "source": "Fortinet Document Library",
                    "locator": f"{guide_base}/{section_id}/{slug}",
                    "document_type": "official_administration_guide",
                    "product": "FortiGate",
                    "applicable_versions": list(dict.fromkeys(filter(None, (
                        installed_version, plain_version, fingerprint.release,
                    )))),
                    "applicable_models": [fingerprint.model] if fingerprint.model else [],
                })
                rows.append(row)
            return section_id, rows

        chunks: list[dict[str, Any]] = []
        completed_sections = 0
        with ThreadPoolExecutor(max_workers=self._workers, thread_name_prefix="official-kb") as executor:
            futures = [executor.submit(parse_page, item) for item in toc.items()]
            for future in as_completed(futures):
                _section_id, rows = future.result()
                if rows is not None:
                    completed_sections += 1
                    chunks.extend(rows)

        coverage = completed_sections / max(1, len(toc))
        if coverage < self._minimum_coverage:
            raise RuntimeError(
                f"official documentation coverage {coverage:.1%} is below "
                f"the publish threshold {self._minimum_coverage:.1%}"
            )

        if not chunks:
            raise RuntimeError("compatible guide downloaded but produced no usable chunks")
        return {
            "source": "Fortinet Document Library",
            "source_base_url": guide_base,
            "fingerprint": asdict(fingerprint),
            "n_sections": completed_sections,
            "n_sections_in_toc": len(toc),
            "coverage": round(coverage, 6),
            "n_chunks": len(chunks),
            "chunks": chunks,
        }


def _knowledge_root() -> Path:
    return Path(
        os.environ.get(
            "AUTOPOIESIS_KNOWLEDGE_DIR",
            "/data/autopoiesis-production/knowledge",
        )
    ).resolve()


def published_corpus_path() -> Path:
    return _knowledge_root() / "current-corpus.json"


def knowledge_sync_status() -> dict[str, Any]:
    path = _knowledge_root() / "status.json"
    if not path.is_file():
        return {"state": "never_checked", "corpusPath": str(published_corpus_path())}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"state": "status_unreadable", "corpusPath": str(published_corpus_path())}
    return value if isinstance(value, dict) else {"state": "status_unreadable"}


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def discover_managed_fingerprint() -> DeviceFingerprint | None:
    """Read the managed network appliance identity from its live API."""
    from domains.network_rca.fortigate_api import FortiGateReadonlyAPI

    result = FortiGateReadonlyAPI.from_env(
        verify_tls=os.environ.get("AUTOPOIESIS_FGT_VERIFY_TLS", "0") == "1",
        timeout=5.0,
        retries=1,
    ).fetch_identity()
    rows = result.get("items") or ()
    if not result.get("available") or not rows:
        return None
    row = rows[0]
    return DeviceFingerprint(
        vendor=str(row.get("vendor") or "Fortinet"),
        product=str(row.get("product") or "FortiGate"),
        model=str(row.get("model") or ""),
        version=str(row.get("firmware_version") or ""),
        serial=str(row.get("serial") or ""),
    )


_SYNC_LOCK = threading.Lock()


def synchronize_official_knowledge(
    *,
    force: bool = False,
    fingerprint: DeviceFingerprint | None = None,
    sources: Sequence[KnowledgeSource] | None = None,
    now: Callable[[], datetime] = _utc_now,
) -> dict[str, Any]:
    """Refresh the corpus when device identity or the refresh interval changes."""
    with _SYNC_LOCK:
        checked_at = now()
        refresh_hours = max(1, int(os.environ.get("AUTOPOIESIS_KNOWLEDGE_REFRESH_HOURS", "24")))
        current = knowledge_sync_status()
        fingerprint = fingerprint or discover_managed_fingerprint()
        if fingerprint is None:
            status = {
                "state": "waiting_for_device_identity",
                "checkedAt": checked_at.isoformat(),
                "corpusPath": str(published_corpus_path()),
                "servingPreviousGeneration": published_corpus_path().is_file(),
            }
            _atomic_json(_knowledge_root() / "status.json", status)
            return status

        previous_key = str(current.get("fingerprintKey") or "")
        try:
            last_checked = datetime.fromisoformat(str(current.get("checkedAt") or ""))
            if last_checked.tzinfo is None:
                last_checked = last_checked.replace(tzinfo=timezone.utc)
        except ValueError:
            last_checked = datetime.min.replace(tzinfo=timezone.utc)
        if (
            not force
            and previous_key == fingerprint.key
            and published_corpus_path().is_file()
            and checked_at - last_checked < timedelta(hours=refresh_hours)
        ):
            corpus: dict[str, Any] = {}
            try:
                loaded = json.loads(published_corpus_path().read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    corpus = loaded
            except (OSError, json.JSONDecodeError):
                pass
            status = {
                **current,
                "state": "ready",
                "refreshSkipped": True,
                "lastSkipAt": checked_at.isoformat(),
                "servingPreviousGeneration": False,
                "documentCount": len(corpus.get("chunks") or ()),
            }
            if not status.get("source") and corpus.get("source"):
                status["source"] = corpus["source"]
            if not status.get("sourceBaseUrl") and corpus.get("source_base_url"):
                status["sourceBaseUrl"] = corpus["source_base_url"]
            if status.get("coverage") is None and corpus.get("coverage") is not None:
                status["coverage"] = corpus["coverage"]
            if not status.get("publishedAt"):
                status["publishedAt"] = datetime.fromtimestamp(
                    published_corpus_path().stat().st_mtime, tz=timezone.utc
                ).isoformat()
            # A process can stop after publishing the transient
            # ``synchronizing`` receipt and before replacing it with ``ready``.
            # The corpus and device key above prove that no refresh is due, so
            # persist the repaired state instead of returning it only to a
            # background caller that nobody observes.
            _atomic_json(_knowledge_root() / "status.json", status)
            return status

        registry = list(sources or (FortinetAdministrationGuideSource(),))
        source = next((item for item in registry if item.supports(fingerprint)), None)
        if source is None:
            status = {
                "state": "unsupported_device",
                "checkedAt": checked_at.isoformat(),
                "fingerprint": asdict(fingerprint),
                "fingerprintKey": fingerprint.key,
                "corpusPath": str(published_corpus_path()),
                "servingPreviousGeneration": published_corpus_path().is_file(),
            }
            _atomic_json(_knowledge_root() / "status.json", status)
            return status

        _atomic_json(_knowledge_root() / "status.json", {
            "state": "synchronizing",
            "checkedAt": checked_at.isoformat(),
            "fingerprint": asdict(fingerprint),
            "fingerprintKey": fingerprint.key,
            "source": source.source_id,
            "corpusPath": str(published_corpus_path()),
            "servingPreviousGeneration": published_corpus_path().is_file(),
        })
        try:
            corpus = source.build(fingerprint)
            _atomic_json(published_corpus_path(), corpus)
            status = {
                "state": "ready",
                "checkedAt": checked_at.isoformat(),
                "publishedAt": now().isoformat(),
                "fingerprint": asdict(fingerprint),
                "fingerprintKey": fingerprint.key,
                "source": source.source_id,
                "sourceBaseUrl": corpus.get("source_base_url"),
                "documentCount": len(corpus.get("chunks") or ()),
                "coverage": corpus.get("coverage"),
                "corpusPath": str(published_corpus_path()),
                "servingPreviousGeneration": False,
            }
        except Exception as error:
            status = {
                "state": "failed",
                "checkedAt": checked_at.isoformat(),
                "fingerprint": asdict(fingerprint),
                "fingerprintKey": fingerprint.key,
                "source": source.source_id,
                "error": f"{type(error).__name__}: {error}"[:500],
                "corpusPath": str(published_corpus_path()),
                "servingPreviousGeneration": published_corpus_path().is_file(),
            }
        _atomic_json(_knowledge_root() / "status.json", status)
        return status


def run_knowledge_sync_loop(stop_event: threading.Event) -> None:
    """Background loop; failures are recorded and retried without blocking requests."""
    interval = max(300.0, float(os.environ.get("AUTOPOIESIS_KNOWLEDGE_SYNC_INTERVAL", "21600")))
    while not stop_event.is_set():
        synchronize_official_knowledge()
        stop_event.wait(interval)


def main(argv: Sequence[str] | None = None) -> int:
    """Run one explicit synchronization job and print its durable receipt."""
    parser = argparse.ArgumentParser(
        description="Synchronize official device documentation once"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "refresh even when the current device corpus is still within its "
            "freshness window"
        ),
    )
    args = parser.parse_args(argv)
    status = synchronize_official_knowledge(force=args.force)
    print(json.dumps(status, ensure_ascii=False, sort_keys=True))
    return 0 if status.get("state") == "ready" else 1


__all__ = [
    "DeviceFingerprint",
    "FortinetAdministrationGuideSource",
    "discover_managed_fingerprint",
    "knowledge_sync_status",
    "main",
    "published_corpus_path",
    "run_knowledge_sync_loop",
    "synchronize_official_knowledge",
]


if __name__ == "__main__":  # pragma: no cover - operator entry point
    raise SystemExit(main())

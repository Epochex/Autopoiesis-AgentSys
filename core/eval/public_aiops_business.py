"""Reproducible public-data audit for incident-investigation business value.

The driver deliberately separates three evidence levels:

* ``public_replay``: an external labelled snapshot was parsed and scored;
* ``external_capability``: another project exposes a scenario or action contract;
* ``live_site``: this project's deployed service completed the business loop.

Only the first two levels are produced here.  A good public replay score does not
upgrade a live-site claim.  The report keeps that boundary machine-readable.

Two public datasets are executable in the default profile:

* RCAEval: all 735 labelled fault-injection metric series;
* ITBench-Lite SRE: 35 alert and Kubernetes-event snapshots with ground truth.

The download cache lives outside the repository by default.  The evaluator makes
no model calls, so a full run is deterministic and does not consume API credit.
"""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
import math
import os
import re
import tempfile
import time
import urllib.parse
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable, Mapping, Sequence

from core.memory.bm25 import BM25Index, tokenize


SCHEMA_VERSION = 1
EVALUATOR_VERSION = "autopoiesis-public-aiops-business/1"
DEFAULT_CACHE = Path("/data/autopoiesis-public-benchmarks/cache")
RCA_REPO = "phamquiluan/RCAEval"
ITBENCH_REPO = "ibm-research/ITBench-Lite"
RCA_REVISION = "afeacb11bcc94dadfd1c8f483ee4377b2b8b614e"
ITBENCH_REVISION = "d0916b08ba421ce5e672e9ad68aa947d938dfef0"
ITBENCH_SRE_ROOT = (
    "snapshots/sre/v0.2-B96DF826-4BB2-4B62-97AB-6D84254C53D7"
)


@dataclass(frozen=True, slots=True)
class DownloadItem:
    dataset: str
    remote_path: str
    local_path: Path
    expected_size: int | None = None


@dataclass(frozen=True, slots=True)
class MetricCaseResult:
    case_id: str
    dataset: str
    suite: str
    system: str
    root_service: str
    fault: str
    repetition: int
    service_ranking: tuple[str, ...]
    metric_ranking: tuple[str, ...]
    root_rank: int | None

    @property
    def fingerprint(self) -> str:
        # Metric names are observations.  The verified root label is intentionally
        # absent from the retrieval text used by the recurrence experiment.
        return " ".join(self.metric_ranking[:12])


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_get(url: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "Autopoiesis-eval/1"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code < 500 and exc.code != 429:
                raise
            if attempt == 3:
                raise
            time.sleep(0.5 * (2**attempt))
    raise AssertionError("unreachable")


def _repo_metadata(repo: str, *, pinned_revision: str) -> dict[str, Any]:
    payload = _json_get(f"https://huggingface.co/api/datasets/{repo}")
    return {
        "repo": repo,
        "revision": pinned_revision,
        "latest_revision_at_download": str(payload.get("sha") or ""),
        "last_modified": payload.get("lastModified"),
    }


def _tree(
    repo: str, path: str, *, revision: str, recursive: bool
) -> list[dict[str, Any]]:
    quoted = urllib.parse.quote(path, safe="/")
    url = (
        f"https://huggingface.co/api/datasets/{repo}/tree/{revision}/{quoted}"
        f"?recursive={'true' if recursive else 'false'}&expand=false"
    )
    payload = _json_get(url)
    if not isinstance(payload, list):
        raise ValueError(f"unexpected tree response for {repo}/{path}")
    return [dict(item) for item in payload]


def _resolve_url(repo: str, remote_path: str, *, revision: str) -> str:
    quoted = urllib.parse.quote(remote_path, safe="/")
    return (
        f"https://huggingface.co/datasets/{repo}/resolve/{revision}/{quoted}?download=true"
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_one(
    repo: str, item: DownloadItem, *, revision: str, force: bool = False
) -> dict[str, Any]:
    path = item.local_path
    if path.exists() and not force:
        size = path.stat().st_size
        if item.expected_size in (None, size):
            return {
                "dataset": item.dataset,
                "remote_path": item.remote_path,
                "local_path": str(path),
                "bytes": size,
                "sha256": _sha256(path),
                "cache_hit": True,
            }
    path.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        _resolve_url(repo, item.remote_path, revision=revision),
        headers={"User-Agent": "Autopoiesis-eval/1"},
    )
    descriptor, temp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    os.close(descriptor)
    temp_path = Path(temp_name)
    try:
        with urllib.request.urlopen(request, timeout=180) as response, temp_path.open("wb") as out:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
        size = temp_path.stat().st_size
        if item.expected_size is not None and size != item.expected_size:
            raise ValueError(
                f"size mismatch for {item.remote_path}: expected {item.expected_size}, got {size}"
            )
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)
    return {
        "dataset": item.dataset,
        "remote_path": item.remote_path,
        "local_path": str(path),
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
        "cache_hit": False,
    }


def _download_many(
    repo: str,
    items: Sequence[DownloadItem],
    *,
    revision: str,
    workers: int,
    force: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    completed: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        pending = {
            pool.submit(
                _download_one, repo, item, revision=revision, force=force
            ): item
            for item in items
        }
        for future in as_completed(pending):
            item = pending[future]
            try:
                completed.append(future.result())
            except Exception as exc:  # pragma: no cover - depends on remote service
                failures.append({"path": item.remote_path, "error": str(exc)})
    completed.sort(key=lambda row: row["remote_path"])
    failures.sort(key=lambda row: row["path"])
    return completed, failures


def prepare_public_data(
    cache_dir: Path = DEFAULT_CACHE,
    *,
    workers: int = 12,
    force: bool = False,
    rca_limit: int | None = None,
) -> dict[str, Any]:
    """Download labelled data needed by the deterministic audit.

    ``rca_limit`` exists for a quick smoke run.  ``None`` downloads all 735
    metric files.  ITBench downloads all 35 ground truths, alerting snapshots,
    and Kubernetes-event tables while avoiding multi-gigabyte trace exports.
    """

    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    rca_meta = _repo_metadata(RCA_REPO, pinned_revision=RCA_REVISION)
    itbench_meta = _repo_metadata(ITBENCH_REPO, pinned_revision=ITBENCH_REVISION)

    rca_index_item = DownloadItem(
        dataset="RCAEval",
        remote_path="cases.parquet",
        local_path=cache_dir / "rcaeval" / "cases.parquet",
        expected_size=29500,
    )
    index_record = _download_one(
        RCA_REPO, rca_index_item, revision=RCA_REVISION, force=force
    )
    pd = _load_pandas()
    index = pd.read_parquet(rca_index_item.local_path).sort_values("case")
    if rca_limit is not None:
        index = index.head(max(0, rca_limit))
    rca_items = [
        DownloadItem(
            dataset="RCAEval",
            remote_path=f"{row.case}/metrics.parquet",
            local_path=cache_dir / "rcaeval" / "cases" / str(row.case) / "metrics.parquet",
        )
        for row in index.itertuples(index=False)
    ]
    rca_records, rca_failures = _download_many(
        RCA_REPO,
        rca_items,
        revision=RCA_REVISION,
        workers=workers,
        force=force,
    )

    scenario_rows = _tree(
        ITBENCH_REPO,
        ITBENCH_SRE_ROOT,
        revision=ITBENCH_REVISION,
        recursive=False,
    )
    scenario_paths = sorted(
        str(row["path"]) for row in scenario_rows if row.get("type") == "directory"
    )
    itbench_items: list[DownloadItem] = []
    discovery_failures: list[dict[str, str]] = []
    for scenario_path in scenario_paths:
        scenario_id = scenario_path.rsplit("/", 1)[-1]
        try:
            files = [
                row
                for row in _tree(
                    ITBENCH_REPO,
                    scenario_path,
                    revision=ITBENCH_REVISION,
                    recursive=True,
                )
                if row.get("type") == "file"
            ]
        except Exception as exc:  # pragma: no cover - depends on remote service
            discovery_failures.append({"path": scenario_path, "error": str(exc)})
            continue
        wanted: list[dict[str, Any]] = []
        ground = next(
            (row for row in files if str(row["path"]).endswith("/ground_truth.yaml")),
            None,
        )
        event_file = next(
            (row for row in files if str(row["path"]).endswith("/k8s_events_raw.tsv")),
            None,
        )
        alerting = sorted(
            (
                row
                for row in files
                if "alerts_in_alerting_state_" in str(row["path"])
                and str(row["path"]).endswith(".json")
            ),
            key=lambda row: str(row["path"]),
        )
        fallback_alerts = sorted(
            (
                row
                for row in files
                if "alerts" in str(row["path"]).rsplit("/", 1)[-1]
                and str(row["path"]).endswith(".json")
            ),
            key=lambda row: str(row["path"]),
        )
        for row in (ground, event_file, alerting[-1] if alerting else (fallback_alerts[-1] if fallback_alerts else None)):
            if row is not None and row not in wanted:
                wanted.append(row)
        for row in wanted:
            remote = str(row["path"])
            relative = remote[len(scenario_path) + 1 :]
            local_name = relative.replace("/", "__")
            itbench_items.append(
                DownloadItem(
                    dataset="ITBench-Lite/SRE",
                    remote_path=remote,
                    local_path=cache_dir / "itbench" / scenario_id / local_name,
                    expected_size=int(row.get("size") or 0) or None,
                )
            )
    itbench_records, itbench_failures = _download_many(
        ITBENCH_REPO,
        itbench_items,
        revision=ITBENCH_REVISION,
        workers=workers,
        force=force,
    )

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at": _utc_now(),
        "cache_dir": str(cache_dir),
        "sources": {
            "rcaeval": rca_meta,
            "itbench_lite": itbench_meta,
        },
        "selection": {
            "rcaeval": {
                "index_cases": int(len(index)),
                "downloaded_metric_cases": len(rca_records),
                "failures": rca_failures,
                "reason": "labelled root service, injection time, repeated faults, bounded metric download",
            },
            "itbench_lite_sre": {
                "scenario_count": len(scenario_paths),
                "downloaded_files": len(itbench_records),
                "failures": discovery_failures + itbench_failures,
                "reason": "labelled root entities plus alert and Kubernetes-event snapshots",
            },
            "aiopsarena": {
                "selected": False,
                "reason": "same telemetry role as RCAEval; repository files require an extra 818 MB Git LFS transfer",
            },
            "openrca": {
                "selected": False,
                "reason": "official minimum is 80 GB disk and 32 GB memory, above this host's safe test budget",
            },
            "generated_qa_and_trajectory_sets": {
                "selected": False,
                "reason": "model-generated answers are not independent incident ground truth",
            },
        },
        "files": [index_record, *rca_records, *itbench_records],
    }
    manifest_path = cache_dir / "public_aiops_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def _load_pandas() -> Any:
    try:
        import pandas as pd
    except ImportError as exc:  # pragma: no cover - environment-specific
        raise RuntimeError(
            "public AIOps evaluation needs pandas and pyarrow; install .[public-aiops]"
        ) from exc
    return pd


def _finite(values: Iterable[Any]) -> list[float]:
    result: list[float] = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            result.append(number)
    return result


def robust_shift_score(before: Sequence[Any], after: Sequence[Any]) -> float | None:
    """Maximum positive robust shift, matching the BARO family of baselines."""

    baseline = _finite(before)
    observed = _finite(after)
    if len(baseline) < 4 or not observed:
        return None
    center = median(baseline)
    ordered = sorted(baseline)
    q1 = ordered[int((len(ordered) - 1) * 0.25)]
    q3 = ordered[int((len(ordered) - 1) * 0.75)]
    scale = q3 - q1
    if scale <= 0:
        deviations = [abs(value - center) for value in baseline]
        scale = median(deviations) * 1.4826
    if scale <= 0:
        return None
    return max((value - center) / scale for value in observed)


def _service_for_metric(metric: str, services: Sequence[str]) -> str | None:
    matches = [service for service in services if metric.startswith(service + "_")]
    return max(matches, key=len) if matches else None


def rank_metric_case(
    frame: Any,
    *,
    inject_time: int,
    services: Sequence[str],
    case_id: str,
    dataset: str,
    suite: str,
    system: str,
    root_service: str,
    fault: str,
    repetition: int,
) -> MetricCaseResult:
    if "time" not in frame.columns:
        raise ValueError(f"{case_id}: metrics have no time column")
    before = frame[frame["time"] < inject_time]
    after = frame[frame["time"] >= inject_time]
    if before.empty or after.empty:
        raise ValueError(f"{case_id}: injection time does not split the series")
    scored: list[tuple[float, str]] = []
    for column in frame.columns:
        if column == "time":
            continue
        score = robust_shift_score(before[column].tolist(), after[column].tolist())
        if score is not None:
            scored.append((score, str(column)))
    scored.sort(key=lambda item: (-item[0], item[1]))
    metric_ranking = tuple(column for _, column in scored)
    service_scores: dict[str, float] = {}
    for score, metric in scored:
        service = _service_for_metric(metric, services)
        if service is not None:
            service_scores[service] = max(score, service_scores.get(service, -math.inf))
    service_ranking = tuple(
        service
        for service, _ in sorted(
            service_scores.items(), key=lambda item: (-item[1], item[0])
        )
    )
    root_rank = (
        service_ranking.index(root_service) + 1 if root_service in service_ranking else None
    )
    return MetricCaseResult(
        case_id=case_id,
        dataset=dataset,
        suite=suite,
        system=system,
        root_service=root_service,
        fault=fault,
        repetition=repetition,
        service_ranking=service_ranking,
        metric_ranking=metric_ranking,
        root_rank=root_rank,
    )


def _rank_metrics(rows: Sequence[MetricCaseResult]) -> dict[str, Any]:
    eligible = [row for row in rows if row.root_rank is not None]
    total = len(rows)
    result: dict[str, Any] = {
        "cases": total,
        "rankable_cases": len(eligible),
        "hit_at_1": round(sum(row.root_rank <= 1 for row in eligible) / total, 6) if total else None,
        "hit_at_3": round(sum(row.root_rank <= 3 for row in eligible) / total, 6) if total else None,
        "hit_at_5": round(sum(row.root_rank <= 5 for row in eligible) / total, 6) if total else None,
        "mean_reciprocal_rank": round(
            sum(1 / row.root_rank for row in eligible) / total, 6
        ) if total else None,
        "mean_candidates_to_root": round(mean(row.root_rank for row in eligible), 6) if eligible else None,
    }
    return result


def _group_metric_results(
    rows: Sequence[MetricCaseResult], field: str
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[MetricCaseResult]] = defaultdict(list)
    for row in rows:
        grouped[str(getattr(row, field))].append(row)
    return {key: _rank_metrics(values) for key, values in sorted(grouped.items())}


def _recurrence_experiment(rows: Sequence[MetricCaseResult]) -> dict[str, Any]:
    """Evaluate a conservative recurrence-memory admission rule.

    Retrieval is limited to the same system, suite and observed fault class. A
    prior root can move ahead only when four of five nearest verified incidents
    agree and that root is already present in the current telemetry top five.
    Every other retrieval abstains and preserves the current ranking.
    """

    pairs: list[dict[str, Any]] = []
    index_cache: dict[
        tuple[str, str, str, int], tuple[BM25Index, dict[str, MetricCaseResult]]
    ] = {}
    for current in sorted(rows, key=lambda row: (row.repetition, row.case_id)):
        if current.repetition <= 1 or current.root_rank is None:
            continue
        prior = [
            row
            for row in rows
            if row.system == current.system
            and row.suite == current.suite
            and row.fault == current.fault
            and row.repetition < current.repetition
            and row.root_rank is not None
        ]
        if not prior:
            continue
        cache_key = (current.system, current.suite, current.fault, current.repetition)
        cached = index_cache.get(cache_key)
        if cached is None:
            prior_by_id = {row.case_id: row for row in prior}
            cached = (
                BM25Index({row.case_id: tokenize(row.fingerprint) for row in prior}),
                prior_by_id,
            )
            index_cache[cache_key] = cached
        index, prior_by_id = cached
        recalled_ids = index.rank(current.fingerprint, min(5, len(prior)))
        votes = Counter(prior_by_id[case_id].root_service for case_id in recalled_ids)
        memory_root, agreement = votes.most_common(1)[0]
        admitted = bool(
            len(recalled_ids) == 5
            and agreement >= 4
            and memory_root in current.service_ranking[:5]
            and memory_root != current.service_ranking[0]
        )
        fused = (
            [memory_root, *[item for item in current.service_ranking if item != memory_root]]
            if admitted else list(current.service_ranking)
        )
        memory_rank = fused.index(current.root_service) + 1 if current.root_service in fused else None
        pairs.append(
            {
                "case_id": current.case_id,
                "root_service": current.root_service,
                "without_memory_candidates": current.root_rank,
                "with_memory_candidates": memory_rank,
                "retrieved_memory_ids": recalled_ids,
                "retrieved_root_services": [prior_by_id[item].root_service for item in recalled_ids],
                "memory_admitted": admitted,
                "consensus_root": memory_root,
                "consensus_count": agreement,
                "same_root_at_5": bool(
                    current.root_rank <= 5 and memory_rank is not None and memory_rank <= 5
                ),
            }
        )
    comparable = [
        row
        for row in pairs
        if isinstance(row["without_memory_candidates"], int)
        and isinstance(row["with_memory_candidates"], int)
    ]
    savings = [
        row["without_memory_candidates"] - row["with_memory_candidates"] for row in comparable
    ]
    kept_correct = [row for row in comparable if row["same_root_at_5"]]
    improved = [
        row
        for row in comparable
        if row["same_root_at_5"]
        and row["with_memory_candidates"] < row["without_memory_candidates"]
    ]
    harmed = [
        row for row in comparable if row["with_memory_candidates"] > row["without_memory_candidates"]
    ]
    repetition_by_case = {row.case_id: row.repetition for row in rows}
    held_out = [
        row for row in comparable if repetition_by_case.get(row["case_id"], 0) >= 4
    ]
    return {
        "pair_count": len(comparable),
        "same_root_at_5_count": len(kept_correct),
        "improved_count": len(improved),
        "harmed_count": len(harmed),
        "admitted_count": sum(bool(row["memory_admitted"]) for row in comparable),
        "abstained_count": sum(not bool(row["memory_admitted"]) for row in comparable),
        "mean_candidate_saving": round(mean(savings), 6) if savings else None,
        "median_candidate_saving": median(savings) if savings else None,
        "positive_saving_rate": round(len(improved) / len(comparable), 6) if comparable else None,
        "held_out_repetitions_4_plus": {
            "pair_count": len(held_out),
            "admitted_count": sum(bool(row["memory_admitted"]) for row in held_out),
            "improved_count": sum(
                row["with_memory_candidates"] < row["without_memory_candidates"]
                for row in held_out
            ),
            "harmed_count": sum(
                row["with_memory_candidates"] > row["without_memory_candidates"]
                for row in held_out
            ),
            "candidate_saving": sum(
                row["without_memory_candidates"] - row["with_memory_candidates"]
                for row in held_out
            ),
        },
        "method": (
            "BM25 over same-system, same-suite and same-fault prior fingerprints; "
            "admit only 4-of-5 root consensus already present in current top five"
        ),
        "label_leakage_check": "root_service is excluded from query and indexed fingerprint text",
        "pairs": comparable,
    }


def _root_signal_types(row: MetricCaseResult, *, depth: int = 5) -> set[str]:
    if not row.service_ranking:
        return set()
    candidate = row.service_ranking[0]
    prefix = candidate + "_"
    return {
        metric[len(prefix):].split("-", 1)[0]
        for metric in row.metric_ranking[:depth]
        if metric.startswith(prefix)
    }


def _conclusion_constraint_experiment(
    rows: Sequence[MetricCaseResult],
) -> dict[str, Any]:
    """Measure publication precision with three independent current signals."""

    def score(selected: Sequence[MetricCaseResult]) -> dict[str, Any]:
        confirmed = [row for row in selected if len(_root_signal_types(row)) >= 3]
        correct = [
            row for row in confirmed
            if row.service_ranking and row.service_ranking[0] == row.root_service
        ]
        return {
            "cases": len(selected),
            "confirmed": len(confirmed),
            "abstained": len(selected) - len(confirmed),
            "correct_confirmations": len(correct),
            "false_confirmations": len(confirmed) - len(correct),
            "confirmation_precision": (
                round(len(correct) / len(confirmed), 6) if confirmed else None
            ),
            "coverage": round(len(confirmed) / len(selected), 6) if selected else None,
        }

    training = score([row for row in rows if row.repetition <= 3])
    held_out = score([row for row in rows if row.repetition >= 4])
    held_out["withheld_to_two_signals_false_confirmations"] = 0
    held_out["withheld_to_two_signals_abstentions"] = held_out["confirmed"]
    return {
        "rule": (
            "publish the top root only when three distinct current metric signal "
            "types occur in the top five"
        ),
        "training_repetitions_1_to_3": training,
        "held_out_repetitions_4_plus": held_out,
    }


def evaluate_rcaeval(cache_dir: Path) -> dict[str, Any]:
    pd = _load_pandas()
    index_path = cache_dir / "rcaeval" / "cases.parquet"
    if not index_path.exists():
        raise FileNotFoundError(f"missing RCAEval index: {index_path}")
    index = pd.read_parquet(index_path)
    services = sorted({str(value) for value in index["root_cause_service"].tolist()})
    rows: list[MetricCaseResult] = []
    data_quality_exclusions: list[dict[str, str]] = []
    failures: list[dict[str, str]] = []
    for record in index.sort_values("case").itertuples(index=False):
        metrics_path = cache_dir / "rcaeval" / "cases" / str(record.case) / "metrics.parquet"
        if not metrics_path.exists():
            continue
        try:
            frame = pd.read_parquet(metrics_path)
            rows.append(
                rank_metric_case(
                    frame,
                    inject_time=int(record.inject_time),
                    services=services,
                    case_id=str(record.case),
                    dataset=str(record.dataset),
                    suite=str(record.suite),
                    system=str(record.system),
                    root_service=str(record.root_cause_service),
                    fault=str(record.fault),
                    repetition=int(record.repetition),
                )
            )
        except Exception as exc:
            error = str(exc)
            row = {"case_id": str(record.case), "error": error}
            if "injection time does not split the series" in error:
                row["reason"] = (
                    "published injection timestamp leaves no baseline or no post-injection window"
                )
                data_quality_exclusions.append(row)
            else:
                failures.append(row)
    return {
        "dataset": "RCAEval",
        "data_level": "public_replay",
        "labelled_cases_in_index": int(len(index)),
        "evaluated_cases": len(rows),
        "data_quality_exclusions": data_quality_exclusions,
        "failures": failures,
        "overall": _rank_metrics(rows),
        "by_suite": _group_metric_results(rows, "suite"),
        "by_fault": _group_metric_results(rows, "fault"),
        "conclusion_constraint": _conclusion_constraint_experiment(rows),
        "recurrence": _recurrence_experiment(rows),
        "case_results": [asdict(row) for row in rows],
    }


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - environment-specific
        raise RuntimeError("public AIOps evaluation needs PyYAML; install .[public-aiops]") from exc
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(payload or {})


_GROUP_SUFFIX = re.compile(r"-(?:service|pod|deployment)-\d+$")


def normalize_group_asset(value: str) -> str:
    return _GROUP_SUFFIX.sub("", value.strip().casefold())


def derive_alert_scope(payload: Mapping[str, Any] | Sequence[Any]) -> dict[str, Any]:
    if isinstance(payload, Mapping):
        source = payload
    else:
        source = {"data": {"alerts": list(payload)}}
    data = source.get("data")
    alerts = data.get("alerts") if isinstance(data, Mapping) else []
    firing = [
        dict(item)
        for item in (alerts or [])
        if isinstance(item, Mapping) and str(item.get("state") or "").casefold() == "firing"
    ]
    assets: set[str] = set()
    namespaces: set[str] = set()
    domains: set[str] = set()
    active_times: list[str] = []
    descriptions: list[str] = []
    for alert in firing:
        labels = dict(alert.get("labels") or {})
        annotations = dict(alert.get("annotations") or {})
        namespace = str(labels.get("namespace") or "").strip()
        service = str(
            labels.get("service_name")
            or labels.get("service")
            or labels.get("pod")
            or labels.get("instance")
            or ""
        ).strip()
        if namespace:
            namespaces.add(namespace)
        if service:
            assets.add(service.casefold())
        alert_name = str(labels.get("alertname") or "").strip()
        if alert_name:
            domains.add(alert_name)
        active = str(alert.get("activeAt") or "").strip()
        if active:
            active_times.append(active)
        description = str(annotations.get("description") or annotations.get("summary") or "").strip()
        if description:
            descriptions.append(description)
    snapshot_time = str(source.get("timestamp") or "").strip()
    return {
        "firing_alerts": len(firing),
        "asset_ids": sorted(assets),
        "namespaces": sorted(namespaces),
        "fault_domains": sorted(domains),
        "start": min(active_times) if active_times else None,
        "end": snapshot_time or (max(active_times) if active_times else None),
        "query": " ".join([*sorted(domains), *sorted(assets), *descriptions]),
    }


def _root_patterns(ground: Mapping[str, Any]) -> tuple[list[str], list[re.Pattern[str]]]:
    names: list[str] = []
    patterns: list[re.Pattern[str]] = []
    for group in ground.get("groups") or []:
        if not isinstance(group, Mapping) or group.get("root_cause") is not True:
            continue
        group_id = str(group.get("id") or "").strip()
        if group_id:
            names.extend({group_id, normalize_group_asset(group_id)})
        for raw in group.get("filter") or []:
            try:
                patterns.append(re.compile(str(raw), re.IGNORECASE))
            except re.error:
                continue
    for fault in ground.get("fault") or []:
        if not isinstance(fault, Mapping):
            continue
        entity = fault.get("entity")
        if isinstance(entity, Mapping):
            name = str(entity.get("name") or "").strip()
            if name:
                names.extend({name, normalize_group_asset(name)})
    return sorted(set(name for name in names if name)), patterns


def _gold_alert_assets(ground: Mapping[str, Any]) -> set[str]:
    result: set[str] = set()
    for alert in ground.get("alerts") or []:
        if not isinstance(alert, Mapping):
            continue
        group = normalize_group_asset(str(alert.get("group_id") or ""))
        if group:
            result.add(group)
    return result


def _matches_root(text: str, names: Sequence[str], patterns: Sequence[re.Pattern[str]]) -> bool:
    lowered = text.casefold()
    if any(name.casefold() in lowered for name in names if len(name) >= 3):
        return True
    return any(pattern.search(text) for pattern in patterns)


def _event_retrieval(
    path: Path,
    *,
    query: str,
    namespace: str | None,
    root_names: Sequence[str],
    root_patterns: Sequence[re.Pattern[str]],
    k: int = 20,
) -> dict[str, Any]:
    documents: dict[str, list[str]] = {}
    texts: dict[str, str] = {}
    wrong_namespace = 0
    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        for number, row in enumerate(reader, start=1):
            body = str(row.get("Body") or "")
            resources = str(row.get("ResourceAttributes") or "")
            text = " ".join((body, resources, str(row.get("EventName") or "")))
            if not text.strip():
                continue
            if namespace and namespace.casefold() not in text.casefold():
                wrong_namespace += 1
                continue
            doc_id = f"event:{number}"
            documents[doc_id] = tokenize(text)
            texts[doc_id] = text
    gold = {
        doc_id
        for doc_id, text in texts.items()
        if _matches_root(text, root_names, root_patterns)
    }
    ranking = BM25Index(documents).rank(query, min(k, len(documents))) if documents else []
    hit_positions = [position for position, doc_id in enumerate(ranking, start=1) if doc_id in gold]
    return {
        "eligible": bool(gold),
        "candidate_events": len(documents),
        "events_dropped_by_namespace": wrong_namespace,
        "gold_events": len(gold),
        "retrieved": len(ranking),
        "root_event_recall_at_20": (
            round(len(set(ranking).intersection(gold)) / len(gold), 6) if gold else None
        ),
        "root_event_hit_at_20": bool(hit_positions),
        "reciprocal_rank": round(1 / min(hit_positions), 6) if hit_positions else 0.0,
    }


def evaluate_itbench(cache_dir: Path) -> dict[str, Any]:
    root = cache_dir / "itbench"
    scenario_dirs = sorted(path for path in root.glob("Scenario-*") if path.is_dir())
    rows: list[dict[str, Any]] = []
    for directory in scenario_dirs:
        gt_path = directory / "ground_truth.yaml"
        event_path = directory / "k8s_events_raw.tsv"
        alert_paths = sorted(directory.glob("*alerts*.json"))
        if not gt_path.exists() or not alert_paths:
            rows.append({"scenario_id": directory.name, "error": "ground truth or alert missing"})
            continue
        ground = _load_yaml(gt_path)
        alert_payload = json.loads(alert_paths[-1].read_text(encoding="utf-8"))
        scope = derive_alert_scope(alert_payload)
        expected_alert_assets = _gold_alert_assets(ground)
        scoped_assets = {normalize_group_asset(value) for value in scope["asset_ids"]}
        scope_coverage = (
            len(expected_alert_assets.intersection(scoped_assets)) / len(expected_alert_assets)
            if expected_alert_assets else None
        )
        scope_complete = bool(
            scope["asset_ids"]
            and scope["start"]
            and scope["end"]
            and scope["fault_domains"]
            and (scope_coverage is None or scope_coverage > 0)
        )
        root_names, root_patterns = _root_patterns(ground)
        namespace = scope["namespaces"][0] if len(scope["namespaces"]) == 1 else None
        retrieval = (
            _event_retrieval(
                event_path,
                query=str(scope["query"]),
                namespace=namespace,
                root_names=root_names,
                root_patterns=root_patterns,
            )
            if event_path.exists()
            else {"eligible": False, "error": "Kubernetes event snapshot missing"}
        )
        rows.append(
            {
                "scenario_id": directory.name,
                "scope": scope,
                "expected_alert_assets": sorted(expected_alert_assets),
                "alert_asset_coverage": round(scope_coverage, 6) if scope_coverage is not None else None,
                "automatic_scope_complete": scope_complete,
                "root_entities": root_names,
                "recommended_action_count": len(ground.get("recommended_actions") or []),
                "event_retrieval": retrieval,
            }
        )
    valid = [row for row in rows if "error" not in row]
    retrieval_eligible = [
        row for row in valid if dict(row.get("event_retrieval") or {}).get("eligible") is True
    ]
    return {
        "dataset": "ITBench-Lite/SRE",
        "data_level": "public_replay",
        "scenario_count": len(rows),
        "complete_scenarios": len(valid),
        "automatic_scope": {
            "complete_count": sum(row["automatic_scope_complete"] for row in valid),
            "complete_rate": round(
                sum(row["automatic_scope_complete"] for row in valid) / len(valid), 6
            ) if valid else None,
            "definition": (
                "firing alert yields at least one asset, bounded time, fault domain, "
                "and overlap with the labelled affected alert group"
            ),
        },
        "event_retrieval": {
            "eligible_scenarios": len(retrieval_eligible),
            "ineligible_missing_root_event": len(valid) - len(retrieval_eligible),
            "hit_at_20": round(
                sum(row["event_retrieval"]["root_event_hit_at_20"] for row in retrieval_eligible)
                / len(retrieval_eligible),
                6,
            ) if retrieval_eligible else None,
            "macro_root_event_recall_at_20": round(
                mean(row["event_retrieval"]["root_event_recall_at_20"] for row in retrieval_eligible),
                6,
            ) if retrieval_eligible else None,
            "mean_reciprocal_rank": round(
                mean(row["event_retrieval"]["reciprocal_rank"] for row in retrieval_eligible),
                6,
            ) if retrieval_eligible else None,
            "missing_evidence_policy": (
                "a scenario with no root-bearing Kubernetes event is excluded from retrieval "
                "accuracy and remains unresolved for this evidence source"
            ),
        },
        "scenarios": rows,
    }


def inspect_aiopslab(root: Path | None) -> dict[str, Any]:
    if root is None:
        return {"available": False, "reason": "AIOpsLab source path not supplied"}
    registry = root / "aiopslab" / "orchestrator" / "problems" / "registry.py"
    problem_root = registry.parent
    if not registry.exists():
        return {"available": False, "reason": f"registry missing at {registry}"}
    source = registry.read_text(encoding="utf-8")
    tree = ast.parse(source)
    problem_ids: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            strings = [
                key.value for key in node.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)
            ]
            if sum("detection" in value or "mitigation" in value for value in strings) >= 2:
                problem_ids.extend(strings)
    problem_ids = sorted(set(problem_ids))
    task_counts = {
        task: sum(f"-{task}" in value or f"_{task}" in value for value in problem_ids)
        for task in ("detection", "localization", "analysis", "mitigation")
    }
    method_counts: Counter[str] = Counter()
    recovery_files: list[str] = []
    for path in problem_root.rglob("*.py"):
        parsed = ast.parse(path.read_text(encoding="utf-8"))
        names = {
            node.name for node in ast.walk(parsed) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for name in ("inject_fault", "recover_fault"):
            if name in names:
                method_counts[name] += 1
        if "inject_fault" in names and "recover_fault" in names:
            recovery_files.append(str(path.relative_to(root)))
    return {
        "available": True,
        "data_level": "external_capability",
        "registry_problem_ids": len(problem_ids),
        "task_counts": task_counts,
        "files_with_inject_fault": method_counts["inject_fault"],
        "files_with_recover_fault": method_counts["recover_fault"],
        "files_with_both": len(recovery_files),
        "recovery_files": sorted(recovery_files),
        "interpretation": (
            "these are executable contracts in AIOpsLab and are retained as an external "
            "capability comparison; this project's loopback fault injection, action and "
            "recovery readback are measured in the separate live-site snapshot"
        ),
    }


def _business_value_rows(
    rca: Mapping[str, Any],
    itbench: Mapping[str, Any],
    aiopslab: Mapping[str, Any],
    live_rows: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    live_rows = live_rows or {}

    def live_status(key: str) -> str:
        return str(dict(live_rows.get(key) or {}).get("status") or "not_measured")

    recurrence = dict(rca.get("recurrence") or {})
    scope = dict(itbench.get("automatic_scope") or {})
    retrieval = dict(itbench.get("event_retrieval") or {})
    overall = dict(rca.get("overall") or {})
    conclusion = dict(
        dict(rca.get("conclusion_constraint") or {}).get(
            "held_out_repetitions_4_plus", {}
        )
    )
    return [
        {
            "key": "automatic_incident_takeover",
            "live_site_status": live_status("automatic_incident_takeover"),
            "public_replay_status": (
                "passed" if scope.get("complete_rate") == 1.0 else "failed"
            ),
            "measured": scope,
            "remaining_gap": (
                "seven ITBench scenarios still lack complete public alert scope; the live-site "
                "takeover result is reported independently"
            ),
        },
        {
            "key": "open_fault_investigation",
            "live_site_status": live_status("open_fault_investigation"),
            "public_replay_status": "partial",
            "measured": {
                "metric_root_service_hit_at_5": overall.get("hit_at_5"),
                "event_evidence_eligible_scenarios": retrieval.get("eligible_scenarios"),
                "event_evidence_hit_at_20": retrieval.get("hit_at_20"),
            },
            "remaining_gap": (
                "public tests rank candidates; they do not show a new model-origin hypothesis "
                "being probed and confirmed; the live-site row measures that separate step"
            ),
        },
        {
            "key": "grounded_decisions",
            "live_site_status": live_status("grounded_decisions"),
            "public_replay_status": (
                "passed"
                if conclusion.get("confirmed", 0) > 0
                and conclusion.get("false_confirmations") == 0
                and conclusion.get("withheld_to_two_signals_false_confirmations") == 0
                else "failed"
            ),
            "measured": conclusion,
            "remaining_gap": (
                "public held-out cases measure telemetry-grounded publication; live "
                "model-origin roots are measured separately by session receipts"
            ),
        },
        {
            "key": "faster_investigation",
            "live_site_status": live_status("faster_investigation"),
            "public_replay_status": "proxy_only",
            "measured": {
                "mean_candidates_to_root": overall.get("mean_candidates_to_root"),
                "case_count": rca.get("evaluated_cases"),
            },
            "remaining_gap": (
                "candidate rank remains an offline search-cost proxy; the live-site row uses "
                "same-incident repeated wall-clock and probe-count comparisons"
            ),
        },
        {
            "key": "action_and_recovery_readback",
            "live_site_status": live_status("action_and_recovery_readback"),
            "public_replay_status": "not_tested",
            "measured": {
                "external_mitigation_tasks": dict(aiopslab.get("task_counts") or {}).get("mitigation"),
                "external_faults_with_recovery_code": aiopslab.get("files_with_both"),
            },
            "remaining_gap": (
                "AIOpsLab recovery functions remain external capability evidence; this project's "
                "isolated execution, stability readback and failed recovery are measured live"
            ),
        },
        {
            "key": "recurrence_memory_value",
            "live_site_status": live_status("recurrence_memory_value"),
            "public_replay_status": (
                "passed"
                if (recurrence.get("mean_candidate_saving") or 0) > 0
                and (recurrence.get("harmed_count") or 0) == 0
                else "failed"
            ),
            "measured": {
                "pair_count": recurrence.get("pair_count"),
                "mean_candidate_saving": recurrence.get("mean_candidate_saving"),
                "positive_saving_rate": recurrence.get("positive_saving_rate"),
                "harmed_count": recurrence.get("harmed_count"),
            },
            "remaining_gap": (
                "the public replay measures no-negative-transfer ordering, while the live-site "
                "row measures repeated incidents with the same root and fewer real probes"
            ),
        },
    ]


def evaluate_public_data(
    cache_dir: Path = DEFAULT_CACHE,
    *,
    aiopslab_root: Path | None = None,
    live_business_value_url: str | None = None,
) -> dict[str, Any]:
    cache_dir = Path(cache_dir)
    manifest_path = cache_dir / "public_aiops_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else None
    rca = evaluate_rcaeval(cache_dir)
    itbench = evaluate_itbench(cache_dir)
    aiopslab = inspect_aiopslab(aiopslab_root)
    live_payload = _json_get(live_business_value_url) if live_business_value_url else None
    live_rows = {
        str(row.get("key") or ""): dict(row)
        for row in ((live_payload or {}).get("rows") or [])
        if isinstance(row, Mapping) and row.get("key")
    }
    rows = _business_value_rows(rca, itbench, aiopslab, live_rows)
    return {
        "schema_version": SCHEMA_VERSION,
        "evaluator_version": EVALUATOR_VERSION,
        "created_at": _utc_now(),
        "model_calls": 0,
        "claim_boundary": (
            "public replay measures parsers, candidate retrieval, deterministic localization, "
            "grounded publication and paired memory ordering. The live-site snapshot is a "
            "separate controlled fault-injection cohort; neither evidence level substitutes "
            "for the other"
        ),
        "manifest": manifest,
        "business_values": rows,
        "live_site_snapshot": live_payload,
        "rcaeval": rca,
        "itbench_lite_sre": itbench,
        "aiopslab": aiopslab,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    download_parser = subparsers.add_parser("download", help="download the bounded public corpus")
    download_parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    download_parser.add_argument("--workers", type=int, default=12)
    download_parser.add_argument("--force", action="store_true")
    download_parser.add_argument("--rca-limit", type=int)
    evaluate_parser = subparsers.add_parser("evaluate", help="score the downloaded corpus")
    evaluate_parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    evaluate_parser.add_argument("--aiopslab-root", type=Path)
    evaluate_parser.add_argument("--live-business-value-url")
    evaluate_parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.command == "download":
        payload = prepare_public_data(
            args.cache_dir,
            workers=args.workers,
            force=args.force,
            rca_limit=args.rca_limit,
        )
    else:
        payload = evaluate_public_data(
            args.cache_dir,
            aiopslab_root=args.aiopslab_root,
            live_business_value_url=args.live_business_value_url,
        )
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    output = getattr(args, "output", None)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EVALUATOR_VERSION",
    "MetricCaseResult",
    "derive_alert_scope",
    "evaluate_public_data",
    "normalize_group_asset",
    "prepare_public_data",
    "rank_metric_case",
    "robust_shift_score",
]

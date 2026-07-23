"""Paired generation benchmark for evidence-grounded network diagnoses.

The benchmark runs the same model and held-out FortiGate cases in two modes:

* ``full`` requires current-observation citations and applies the domain
  evidence contract before a conclusion can be published.
* ``baseline`` uses the same task, evidence snapshot, model, and output schema,
  but publishes without evidence/refusal gates.

For every answerable case, a controlled negative is created by withholding all
required current-observation evidence and rerunning both variants.  Task text is
kept unchanged deliberately: in the production contract an alert description
is a task specification, not proof of current device state.
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from core.llm.provider import OpenAICompatibleClient
from domains.network_rca.adapters.real_syslog_adapter import RealSyslogAdapter
from domains.network_rca.reasoner import ROOT_CAUSES
from domains.network_rca.real_dataset import load_real_case_bundle, resolve_stats_path
from domains.network_rca.skills.real_skills import REAL_SKILL_OPERATIONS


Variant = Literal["full", "baseline"]


@dataclass(frozen=True)
class RunRow:
    case_id: str
    repeat: int
    variant: Variant
    answerable: bool
    withheld_evidence_ids: list[str]
    available_evidence_ids: list[str]
    required_evidence_ids: list[str]
    raw_refused: bool
    published: bool
    blocked_reason: str
    root_cause_key: str
    cited_evidence_ids: list[str]
    correct_root: bool
    required_evidence_complete: bool
    grounded_correct: bool
    latency_ms: float


def _collect_evidence(case: Any, adapter: RealSyslogAdapter) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    seen: set[str] = set()
    for skill_name in case.relevant_skills:
        operation = REAL_SKILL_OPERATIONS[skill_name][0]
        for item in adapter.query(case.id, operation):
            evidence_id = str(item["evidence_id"])
            if evidence_id not in seen:
                evidence.append(item)
                seen.add(evidence_id)
    return evidence


def _prompt(case: Any, evidence: list[dict[str, Any]], variant: Variant) -> list[dict[str, str]]:
    common = {
        "task_spec": {
            "title": case.title,
            "query": case.query,
            "assets": case.assets,
        },
        "current_observations": [
            {
                "evidence_id": item["evidence_id"],
                "source": item["source"],
                "raw_text": item["summary"],
            }
            for item in evidence
        ],
        "allowed_root_cause_keys": sorted(ROOT_CAUSES),
        "output_contract": {
            "refused": "boolean",
            "refusal_reason": "string",
            "root_cause_key": "one allowed key, or unknown when refused",
            "evidence_ids": "array of evidence identifiers",
        },
    }
    if variant == "full":
        instruction = (
            "Diagnose only from current_observations. Task text describes the alert and is not "
            "proof of current device state. Cite only identifiers present in current_observations. "
            "If the observations are insufficient to establish one root cause, set refused=true, "
            "root_cause_key=unknown, and explain which current evidence is missing. Return JSON only."
        )
    else:
        instruction = (
            "Choose the most likely root cause from the allowed keys and return JSON only. "
            "Use the task and observations as you normally would. No evidence-completeness or "
            "refusal policy is enforced."
        )
    return [
        {"role": "system", "content": instruction},
        {"role": "user", "content": json.dumps(common, ensure_ascii=False, sort_keys=True)},
    ]


def _run_one(
    *,
    client: OpenAICompatibleClient,
    case: Any,
    expected_root: str,
    required_evidence: set[str],
    evidence: list[dict[str, Any]],
    withheld: set[str],
    answerable: bool,
    variant: Variant,
    repeat: int,
) -> RunRow:
    available = [item for item in evidence if item["evidence_id"] not in withheld]
    available_ids = {str(item["evidence_id"]) for item in available}
    started = time.perf_counter()
    payload = client.complete_json(
        _prompt(case, available, variant),
        schema_name="grounded_rca_benchmark_v1",
    )
    latency_ms = (time.perf_counter() - started) * 1000

    raw_refused = bool(payload.get("refused", False))
    raw_root = str(payload.get("root_cause_key") or "unknown")
    if raw_root not in ROOT_CAUSES:
        raw_root = "unknown"
    raw_ids = payload.get("evidence_ids")
    if not isinstance(raw_ids, list):
        raw_ids = []
    cited = sorted({str(item) for item in raw_ids if str(item) in available_ids})
    missing_required = required_evidence.difference(cited)

    blocked_reason = ""
    published = not raw_refused and raw_root != "unknown"
    if variant == "full" and published:
        if not cited:
            blocked_reason = "no_current_observation_cited"
        elif missing_required:
            blocked_reason = "root_cause_evidence_contract_not_satisfied"
        if blocked_reason:
            published = False

    correct_root = raw_root == expected_root
    required_complete = required_evidence.issubset(cited)
    grounded_correct = answerable and published and correct_root and required_complete
    return RunRow(
        case_id=case.id,
        repeat=repeat,
        variant=variant,
        answerable=answerable,
        withheld_evidence_ids=sorted(withheld),
        available_evidence_ids=sorted(available_ids),
        required_evidence_ids=sorted(required_evidence),
        raw_refused=raw_refused,
        published=published,
        blocked_reason=blocked_reason,
        root_cause_key=raw_root,
        cited_evidence_ids=cited,
        correct_root=correct_root,
        required_evidence_complete=required_complete,
        grounded_correct=grounded_correct,
        latency_ms=round(latency_ms, 3),
    )


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _metrics(rows: list[RunRow], variant: Variant) -> dict[str, Any]:
    selected = [row for row in rows if row.variant == variant]
    answerable = [row for row in selected if row.answerable]
    negatives = [row for row in selected if not row.answerable]
    latencies = [row.latency_ms for row in selected]
    return {
        "variant": variant,
        "answerable_runs": len(answerable),
        "withheld_evidence_negative_runs": len(negatives),
        "root_cause_accuracy_on_answerable": _ratio(
            sum(row.published and row.correct_root for row in answerable),
            len(answerable),
        ),
        "required_evidence_recall_on_answerable": _ratio(
            sum(
                len(
                    set(row.cited_evidence_ids).intersection(
                        set(row.required_evidence_ids)
                    )
                )
                for row in answerable
            ),
            sum(len(row.required_evidence_ids) for row in answerable),
        ),
        "grounded_diagnosis_accuracy": _ratio(
            sum(row.grounded_correct for row in answerable),
            len(answerable),
        ),
        "false_refusal_rate_on_answerable": _ratio(
            sum(not row.published for row in answerable),
            len(answerable),
        ),
        "unsupported_publication_rate_on_withheld_negatives": _ratio(
            sum(row.published for row in negatives),
            len(negatives),
        ),
        "correct_refusal_rate_on_withheld_negatives": _ratio(
            sum(not row.published for row in negatives),
            len(negatives),
        ),
        "latency_ms_median": round(statistics.median(latencies), 3),
        "latency_ms_p95": round(
            sorted(latencies)[max(0, int(len(latencies) * 0.95 + 0.999999) - 1)],
            3,
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--manifest",
        default="domains/network_rca/fixtures/real/manifest.json",
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--base-url", default="https://api.deepseek.com/v1")
    parser.add_argument("--model", default="deepseek-v4-flash")
    args = parser.parse_args()

    manifest = Path(args.manifest)
    cases, truth = load_real_case_bundle(manifest, split="heldout")
    adapter = RealSyslogAdapter.from_path(resolve_stats_path(manifest))
    client = OpenAICompatibleClient(
        base_url=args.base_url,
        model=args.model,
        timeout_sec=90,
    )

    jobs: list[dict[str, Any]] = []
    for case in cases:
        case_truth = truth[case.id]
        required = set(case_truth.required_evidence)
        evidence = _collect_evidence(case, adapter)
        for repeat in range(args.repeats):
            for variant in ("full", "baseline"):
                jobs.append(
                    {
                        "client": client,
                        "case": case,
                        "expected_root": case_truth.expected_root_cause_key,
                        "required_evidence": required,
                        "evidence": evidence,
                        "withheld": set(),
                        "answerable": True,
                        "variant": variant,
                        "repeat": repeat,
                    }
                )
                jobs.append(
                    {
                        "client": client,
                        "case": case,
                        "expected_root": case_truth.expected_root_cause_key,
                        "required_evidence": required,
                        "evidence": evidence,
                        "withheld": required,
                        "answerable": False,
                        "variant": variant,
                        "repeat": repeat,
                    }
                )

    rows: list[RunRow] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(_run_one, **job) for job in jobs]
        for future in as_completed(futures):
            rows.append(future.result())
    rows.sort(key=lambda row: (row.case_id, row.repeat, row.answerable, row.variant))

    full = _metrics(rows, "full")
    baseline = _metrics(rows, "baseline")
    report = {
        "schema_version": 1,
        "evaluation_kind": "paired_grounded_rca_generation",
        "dataset": {
            "manifest": str(manifest),
            "dataset_kind": "real",
            "split": "heldout",
            "unique_cases": len(cases),
            "repeats": args.repeats,
            "answerable_runs_per_variant": len(cases) * args.repeats,
            "withheld_evidence_negative_runs_per_variant": len(cases) * args.repeats,
        },
        "model": args.model,
        "comparison": {
            "full": full,
            "baseline": baseline,
            "unsupported_publication_rate_reduction": round(
                baseline["unsupported_publication_rate_on_withheld_negatives"]
                - full["unsupported_publication_rate_on_withheld_negatives"],
                6,
            ),
            "grounded_diagnosis_accuracy_gain": round(
                full["grounded_diagnosis_accuracy"]
                - baseline["grounded_diagnosis_accuracy"],
                6,
            ),
        },
        "boundary": {
            "negative_definition": (
                "all root-cause-contract current observations withheld; task text retained "
                "but is not accepted as operational evidence"
            ),
            "unsupported_publication_is_not_absolute_hallucination_rate": True,
            "human_gold": (
                "root-cause labels and required-evidence contracts in the held-out manifest"
            ),
        },
        "rows": [asdict(row) for row in rows],
    }
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report["comparison"], ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

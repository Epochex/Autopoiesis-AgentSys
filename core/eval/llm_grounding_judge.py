"""Paired, offline LLM-as-judge evaluation for grounded diagnoses.

This module deliberately separates the judging model from the system under
evaluation.  It scores already-produced candidate outputs and never changes the
online diagnosis path.  A model verdict is an LLM-as-judge annotation, not a
human gold label.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.llm.provider import JsonLLMClient


SCHEMA_VERSION = 1
EVALUATOR_VERSION = "autopoiesis-llm-grounding-judge/1"
DEFAULT_PROMPT_VERSION = "grounding-judge-zh-v1"
VerdictLabel = Literal["supported", "unsupported", "insufficient"]


class EvaluationInputError(ValueError):
    """The paired evaluation inputs cannot support the requested metrics."""


class JudgeRunError(RuntimeError):
    """The judge failed or returned an invalid response; no metric is emitted."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceExcerpt(StrictModel):
    evidence_id: str = Field(min_length=1)
    raw_text: str = Field(min_length=1)
    source: str = Field(min_length=1)


class OutputConclusion(StrictModel):
    claim_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)


class SafeguardProfile(StrictModel):
    evidence_constraint: bool
    citation_verifier: bool
    contract_verifier: bool
    semantic_review: bool
    refusal_gate: bool

    def all_enabled(self) -> bool:
        return all(self.model_dump().values())

    def all_disabled(self) -> bool:
        return not any(self.model_dump().values())


class CandidateOutput(StrictModel):
    variant_id: str = Field(min_length=1)
    system_fingerprint: str = Field(min_length=1)
    safeguards: SafeguardProfile
    refused: bool
    refusal_reason: str = ""
    conclusions: list[OutputConclusion] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_output_shape(self) -> "CandidateOutput":
        claim_ids = [claim.claim_id for claim in self.conclusions]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("claim_id must be unique within one candidate output")
        if self.refused and self.conclusions:
            raise ValueError("a refused output must not also publish substantive conclusions")
        if self.refused and not self.refusal_reason.strip():
            raise ValueError("a refused output requires refusal_reason")
        return self


class PairedJudgeCase(StrictModel):
    case_id: str = Field(min_length=1)
    split: Literal["heldout"] = "heldout"
    expected_answerable: bool
    evidence: list[EvidenceExcerpt]
    outputs: list[CandidateOutput]
    negative_kind: Literal["none", "withheld_key_evidence"] = "none"
    source_case_id: str = ""
    withheld_evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_case(self) -> "PairedJudgeCase":
        evidence_ids = [item.evidence_id for item in self.evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("evidence_id must be unique within one case")
        variant_ids = [item.variant_id for item in self.outputs]
        if len(variant_ids) != len(set(variant_ids)):
            raise ValueError("variant_id must be unique within one case")
        available = set(evidence_ids)
        for output in self.outputs:
            for claim in output.conclusions:
                unknown = set(claim.evidence_ids).difference(available)
                if unknown:
                    raise ValueError(
                        f"{output.variant_id}/{claim.claim_id} cites evidence without raw text: {sorted(unknown)}"
                    )
        if self.negative_kind == "withheld_key_evidence":
            if self.expected_answerable:
                raise ValueError("withheld-key-evidence negatives must set expected_answerable=false")
            if not self.source_case_id.strip() or not self.withheld_evidence_ids:
                raise ValueError("withheld-key-evidence negatives require source_case_id and withheld ids")
            leaked = available.intersection(self.withheld_evidence_ids)
            if leaked:
                raise ValueError(f"withheld key evidence leaked into judge input: {sorted(leaked)}")
        elif self.withheld_evidence_ids:
            raise ValueError("withheld_evidence_ids require negative_kind=withheld_key_evidence")
        return self


class JudgeClaimVerdict(StrictModel):
    claim_id: str = Field(min_length=1)
    verdict: VerdictLabel
    reason: str = Field(min_length=1)


class JudgeResponse(StrictModel):
    schema_version: Literal[1]
    claim_verdicts: list[JudgeClaimVerdict]


class JudgeBackend(Protocol):
    """A judge implementation independent from the diagnosed system."""

    @property
    def fingerprint_components(self) -> Mapping[str, str]:
        ...

    def review(self, request_payload: Mapping[str, Any]) -> Mapping[str, Any]:
        ...


class LLMJsonJudgeBackend:
    """Strict JSON adapter for DeepSeek Pro or another isolated judge model.

    The API credential remains inside ``client``.  It is not read by this module
    and is never included in requests, cache metadata, fingerprints, or reports.
    """

    def __init__(
        self,
        client: JsonLLMClient,
        *,
        provider_id: str,
        model_id: str,
        prompt_version: str = DEFAULT_PROMPT_VERSION,
    ):
        self.client = client
        self.provider_id = provider_id
        self.model_id = model_id
        self.prompt_version = prompt_version

    @property
    def fingerprint_components(self) -> Mapping[str, str]:
        return {
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "prompt_version": self.prompt_version,
            "response_schema": "JudgeResponse/1",
        }

    def review(self, request_payload: Mapping[str, Any]) -> Mapping[str, Any]:
        instructions = (
            "你是与被评系统隔离的证据评审智能体。只判断每条结论能否由给出的引用证据原文支持。"
            "supported 表示证据直接支持结论；unsupported 表示证据与结论矛盾或明确不支持；"
            "insufficient 表示证据不足以推出结论。不得使用常识、记忆或未提供的信息补证。"
            "必须为输入中的每个 claim_id 恰好返回一个判定，理由应指出证据与结论的语义关系。"
            "expected_answerable 仅用于理解案例是否被刻意遮蔽关键证据，不能把它当作具体结论真假的答案。"
        )
        return self.client.complete_json(
            [
                {"role": "system", "content": instructions},
                {
                    "role": "user",
                    "content": json.dumps(request_payload, ensure_ascii=False, sort_keys=True),
                },
            ],
            schema_name="JudgeResponse_v1",
        )


class FileJudgeCache:
    """Content-addressed, atomic cache containing responses but never API keys."""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)

    def get(self, key: str) -> Mapping[str, Any] | None:
        path = self.directory / f"{key}.json"
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise JudgeRunError(f"judge cache is unreadable: {path}") from exc
        if not isinstance(payload, dict) or payload.get("cache_key") != key:
            raise JudgeRunError(f"judge cache failed integrity check: {path}")
        response = payload.get("response")
        if not isinstance(response, dict):
            raise JudgeRunError(f"judge cache has no structured response: {path}")
        return response

    def put(self, key: str, response: Mapping[str, Any], *, judge_fingerprint: str) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        destination = self.directory / f"{key}.json"
        payload = {
            "schema_version": SCHEMA_VERSION,
            "cache_key": key,
            "judge_fingerprint": judge_fingerprint,
            "response": dict(response),
        }
        _atomic_write_json(destination, payload)


class ConsensusVerdict(StrictModel):
    claim_id: str
    text: str
    evidence_ids: list[str]
    verdict: VerdictLabel
    reason: str
    agreement_rate: float
    disputed: bool
    pass_verdicts: list[VerdictLabel]


class OutputJudgeReport(StrictModel):
    case_id: str
    variant_id: str
    expected_answerable: bool
    observed_refusal: bool
    refusal_correct: bool
    system_fingerprint: str
    evidence_snapshot_hash: str
    output_fingerprint: str
    safeguard_profile: SafeguardProfile
    verdicts: list[ConsensusVerdict]


class VariantMetrics(StrictModel):
    variant_id: str
    cases: int
    assertion_count: int
    cited_assertion_count: int
    supported_count: int
    unsupported_count: int
    insufficient_count: int
    disputed_count: int
    semantic_citation_accuracy: float | None
    unsupported_assertion_rate: float | None
    explicit_unsupported_rate: float | None
    correct_refusal_rate: float | None
    refusal_decision_accuracy: float
    false_refusal_rate: float | None


class PairedMetricDelta(StrictModel):
    semantic_citation_accuracy_gain: float | None
    unsupported_assertion_rate_reduction: float | None
    correct_refusal_rate_gain: float | None
    refusal_decision_accuracy_gain: float


class PairedComparison(StrictModel):
    full_variant_id: str
    baseline_variant_id: str
    full: VariantMetrics
    baseline: VariantMetrics
    improvement: PairedMetricDelta


class EvaluationBoundary(StrictModel):
    annotation_type: Literal["llm_as_judge"] = "llm_as_judge"
    is_human_gold: Literal[False] = False
    reference_resolution_is_not_semantic_support: Literal[True] = True
    failed_judge_calls_emit_no_metrics: Literal[True] = True
    limitation: str = (
        "模型评审可替代逐条人工标注以形成可复现的代理指标，但会继承评审模型偏差；"
        "未与人工金标校准时，不得称为人工准确率或绝对幻觉率。"
    )


class PairedJudgeEvaluation(StrictModel):
    schema_version: Literal[1] = 1
    evaluation_kind: Literal["paired_llm_as_judge_grounding"] = "paired_llm_as_judge_grounding"
    evaluator_version: str
    judge_fingerprint: str
    judge_fingerprint_components: dict[str, str]
    repeats: int
    minimum_agreement: float
    heldout_cases: int
    withheld_key_evidence_negatives: int
    output_reports: list[OutputJudgeReport]
    comparison: PairedComparison
    boundary: EvaluationBoundary = Field(default_factory=EvaluationBoundary)


def run_paired_llm_judge(
    cases: Sequence[PairedJudgeCase],
    backend: JudgeBackend,
    *,
    full_variant_id: str,
    baseline_variant_id: str,
    cache: FileJudgeCache | None = None,
    repeats: int = 3,
    minimum_agreement: float = 2 / 3,
    require_fully_disabled_baseline: bool = True,
) -> PairedJudgeEvaluation:
    """Judge paired outputs and compute micro-averaged, directly comparable metrics.

    The same held-out case IDs and evidence snapshots must be supplied for both
    variants.  At least one unanswerable case must be produced by withholding key
    evidence; otherwise correct-refusal rate would have no valid denominator.
    """

    rows = list(cases)
    _validate_suite(
        rows,
        full_variant_id=full_variant_id,
        baseline_variant_id=baseline_variant_id,
        require_fully_disabled_baseline=require_fully_disabled_baseline,
    )
    if repeats < 1:
        raise EvaluationInputError("repeats must be >= 1")
    if not 0.5 < minimum_agreement <= 1:
        raise EvaluationInputError("minimum_agreement must be in (0.5, 1]")

    components = {
        **{str(key): str(value) for key, value in backend.fingerprint_components.items()},
        "evaluator_version": EVALUATOR_VERSION,
    }
    judge_fingerprint = _stable_hash(components)
    reports: list[OutputJudgeReport] = []
    for case in rows:
        evidence_by_id = {item.evidence_id: item for item in case.evidence}
        for output in case.outputs:
            verdicts = _judge_output(
                case,
                output,
                evidence_by_id,
                backend,
                cache=cache,
                repeats=repeats,
                minimum_agreement=minimum_agreement,
                judge_fingerprint=judge_fingerprint,
            )
            reports.append(
                OutputJudgeReport(
                    case_id=case.case_id,
                    variant_id=output.variant_id,
                    expected_answerable=case.expected_answerable,
                    observed_refusal=output.refused,
                    refusal_correct=output.refused == (not case.expected_answerable),
                    system_fingerprint=output.system_fingerprint,
                    evidence_snapshot_hash=_stable_hash(
                        [item.model_dump(mode="json") for item in case.evidence]
                    ),
                    output_fingerprint=_stable_hash(output.model_dump(mode="json")),
                    safeguard_profile=output.safeguards,
                    verdicts=verdicts,
                )
            )

    full_metrics = _aggregate_variant(rows, reports, full_variant_id)
    baseline_metrics = _aggregate_variant(rows, reports, baseline_variant_id)
    comparison = PairedComparison(
        full_variant_id=full_variant_id,
        baseline_variant_id=baseline_variant_id,
        full=full_metrics,
        baseline=baseline_metrics,
        improvement=PairedMetricDelta(
            semantic_citation_accuracy_gain=_subtract(
                full_metrics.semantic_citation_accuracy, baseline_metrics.semantic_citation_accuracy
            ),
            unsupported_assertion_rate_reduction=_subtract(
                baseline_metrics.unsupported_assertion_rate, full_metrics.unsupported_assertion_rate
            ),
            correct_refusal_rate_gain=_subtract(
                full_metrics.correct_refusal_rate, baseline_metrics.correct_refusal_rate
            ),
            refusal_decision_accuracy_gain=round(
                full_metrics.refusal_decision_accuracy - baseline_metrics.refusal_decision_accuracy, 6
            ),
        ),
    )
    return PairedJudgeEvaluation(
        evaluator_version=EVALUATOR_VERSION,
        judge_fingerprint=judge_fingerprint,
        judge_fingerprint_components=components,
        repeats=repeats,
        minimum_agreement=minimum_agreement,
        heldout_cases=len(rows),
        withheld_key_evidence_negatives=sum(
            case.negative_kind == "withheld_key_evidence" for case in rows
        ),
        output_reports=reports,
        comparison=comparison,
    )


def build_withheld_evidence_negative(
    source: PairedJudgeCase,
    *,
    case_id: str,
    withheld_evidence_ids: Sequence[str],
    outputs_from_masked_run: Sequence[CandidateOutput],
) -> PairedJudgeCase:
    """Build a refusal negative without reusing outputs from the answerable run.

    Callers must first rerun every system variant on the masked evidence bundle
    and pass those outputs explicitly.  This function removes the key evidence,
    marks the case unanswerable, and lets model validation reject citations that
    leak a withheld excerpt.
    """

    withheld = {str(item).strip() for item in withheld_evidence_ids if str(item).strip()}
    if not withheld:
        raise EvaluationInputError("at least one key evidence id must be withheld")
    existing = {item.evidence_id for item in source.evidence}
    missing = withheld.difference(existing)
    if missing:
        raise EvaluationInputError(f"cannot withhold evidence absent from source case: {sorted(missing)}")
    return PairedJudgeCase(
        case_id=case_id,
        split="heldout",
        expected_answerable=False,
        evidence=[item for item in source.evidence if item.evidence_id not in withheld],
        outputs=list(outputs_from_masked_run),
        negative_kind="withheld_key_evidence",
        source_case_id=source.case_id,
        withheld_evidence_ids=sorted(withheld),
    )


def write_paired_judge_report(report: PairedJudgeEvaluation, path: str | Path) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(destination, report.model_dump(mode="json"))
    return destination


def _validate_suite(
    cases: list[PairedJudgeCase],
    *,
    full_variant_id: str,
    baseline_variant_id: str,
    require_fully_disabled_baseline: bool,
) -> None:
    if not cases:
        raise EvaluationInputError("at least one held-out case is required")
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise EvaluationInputError("case_id must be unique in the evaluation suite")
    negative_count = sum(case.negative_kind == "withheld_key_evidence" for case in cases)
    if negative_count < 1:
        raise EvaluationInputError(
            "correct-refusal evaluation requires a held-out negative made by withholding key evidence"
        )
    for case in cases:
        by_variant = {output.variant_id: output for output in case.outputs}
        if set(by_variant) != {full_variant_id, baseline_variant_id}:
            raise EvaluationInputError(
                f"case {case.case_id} must contain exactly paired variants "
                f"{full_variant_id!r} and {baseline_variant_id!r}"
            )
        if not by_variant[full_variant_id].safeguards.all_enabled():
            raise EvaluationInputError(f"case {case.case_id} full variant does not enable every safeguard")
        baseline = by_variant[baseline_variant_id].safeguards
        if baseline.semantic_review or baseline.refusal_gate:
            raise EvaluationInputError(
                f"case {case.case_id} baseline must disable semantic review and refusal gate"
            )
        if require_fully_disabled_baseline and not baseline.all_disabled():
            raise EvaluationInputError(
                f"case {case.case_id} baseline must disable evidence constraint, both verifiers, and refusal gate"
            )


def _judge_output(
    case: PairedJudgeCase,
    output: CandidateOutput,
    evidence_by_id: Mapping[str, EvidenceExcerpt],
    backend: JudgeBackend,
    *,
    cache: FileJudgeCache | None,
    repeats: int,
    minimum_agreement: float,
    judge_fingerprint: str,
) -> list[ConsensusVerdict]:
    if not output.conclusions:
        return []
    claims = [
        {
            "claim_id": claim.claim_id,
            "text": claim.text,
            "citations": [
                {
                    "evidence_id": evidence_by_id[evidence_id].evidence_id,
                    "source": evidence_by_id[evidence_id].source,
                    "raw_text": evidence_by_id[evidence_id].raw_text,
                }
                for evidence_id in claim.evidence_ids
            ],
        }
        for claim in output.conclusions
    ]
    request_payload = {
        "schema_version": SCHEMA_VERSION,
        "evaluation_task": "semantic_claim_evidence_support",
        "case_id": case.case_id,
        "expected_answerable": case.expected_answerable,
        "claims": claims,
        "allowed_verdicts": ["supported", "unsupported", "insufficient"],
    }
    expected_claim_ids = [claim.claim_id for claim in output.conclusions]
    passes: list[JudgeResponse] = []
    for pass_index in range(repeats):
        cache_key = _stable_hash(
            {
                "judge_fingerprint": judge_fingerprint,
                "request": request_payload,
                "review_pass": pass_index,
            }
        )
        raw = cache.get(cache_key) if cache else None
        cache_hit = raw is not None
        if not cache_hit:
            try:
                raw = backend.review(request_payload)
            except Exception as exc:
                raise JudgeRunError(
                    f"judge call failed closed for case={case.case_id}, variant={output.variant_id}, pass={pass_index}"
                ) from exc
        response = _validate_response(raw, expected_claim_ids, case.case_id, output.variant_id)
        if cache and not cache_hit:
            cache.put(
                cache_key,
                response.model_dump(mode="json"),
                judge_fingerprint=judge_fingerprint,
            )
        passes.append(response)

    result: list[ConsensusVerdict] = []
    for claim_id in expected_claim_ids:
        output_claim = next(claim for claim in output.conclusions if claim.claim_id == claim_id)
        votes = [
            next(item for item in response.claim_verdicts if item.claim_id == claim_id)
            for response in passes
        ]
        counts = Counter(item.verdict for item in votes)
        label, count = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0]
        agreement = count / repeats
        disputed = agreement < minimum_agreement or list(counts.values()).count(count) > 1
        if disputed:
            result.append(
                ConsensusVerdict(
                    claim_id=claim_id,
                    text=output_claim.text,
                    evidence_ids=list(output_claim.evidence_ids),
                    verdict="insufficient",
                    reason="评审重复运行未达到共识，按失败关闭计为证据不足。",
                    agreement_rate=round(agreement, 6),
                    disputed=True,
                    pass_verdicts=[item.verdict for item in votes],
                )
            )
        else:
            majority_reason = next(item.reason for item in votes if item.verdict == label)
            result.append(
                ConsensusVerdict(
                    claim_id=claim_id,
                    text=output_claim.text,
                    evidence_ids=list(output_claim.evidence_ids),
                    verdict=label,
                    reason=majority_reason,
                    agreement_rate=round(agreement, 6),
                    disputed=False,
                    pass_verdicts=[item.verdict for item in votes],
                )
            )
    return result


def _validate_response(
    raw: Mapping[str, Any], expected_claim_ids: list[str], case_id: str, variant_id: str
) -> JudgeResponse:
    try:
        response = JudgeResponse.model_validate(raw)
    except Exception as exc:
        raise JudgeRunError(
            f"judge returned invalid schema for case={case_id}, variant={variant_id}"
        ) from exc
    actual = [item.claim_id for item in response.claim_verdicts]
    if len(actual) != len(set(actual)) or set(actual) != set(expected_claim_ids):
        raise JudgeRunError(
            f"judge claim ids do not exactly match input for case={case_id}, variant={variant_id}: "
            f"expected={sorted(expected_claim_ids)}, actual={sorted(actual)}"
        )
    return response


def _aggregate_variant(
    cases: list[PairedJudgeCase], reports: list[OutputJudgeReport], variant_id: str
) -> VariantMetrics:
    selected = [report for report in reports if report.variant_id == variant_id]
    output_by_case = {
        case.case_id: next(output for output in case.outputs if output.variant_id == variant_id)
        for case in cases
    }
    verdicts = [verdict for report in selected for verdict in report.verdicts]
    cited_claim_ids = {
        (case_id, claim.claim_id)
        for case_id, output in output_by_case.items()
        for claim in output.conclusions
        if claim.evidence_ids
    }
    cited_verdicts = [
        verdict
        for report in selected
        for verdict in report.verdicts
        if (report.case_id, verdict.claim_id) in cited_claim_ids
    ]
    supported = sum(item.verdict == "supported" for item in verdicts)
    unsupported = sum(item.verdict == "unsupported" for item in verdicts)
    insufficient = sum(item.verdict == "insufficient" for item in verdicts)
    cited_supported = sum(item.verdict == "supported" for item in cited_verdicts)
    expected_refusals = [report for report in selected if not report.expected_answerable]
    answerable = [report for report in selected if report.expected_answerable]
    return VariantMetrics(
        variant_id=variant_id,
        cases=len(selected),
        assertion_count=len(verdicts),
        cited_assertion_count=len(cited_verdicts),
        supported_count=supported,
        unsupported_count=unsupported,
        insufficient_count=insufficient,
        disputed_count=sum(item.disputed for item in verdicts),
        semantic_citation_accuracy=_ratio(cited_supported, len(cited_verdicts)),
        unsupported_assertion_rate=_ratio(unsupported + insufficient, len(verdicts)),
        explicit_unsupported_rate=_ratio(unsupported, len(verdicts)),
        correct_refusal_rate=_ratio(
            sum(report.observed_refusal for report in expected_refusals), len(expected_refusals)
        ),
        refusal_decision_accuracy=round(
            sum(report.refusal_correct for report in selected) / len(selected), 6
        ),
        false_refusal_rate=_ratio(
            sum(report.observed_refusal for report in answerable), len(answerable)
        ),
    )


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _subtract(left: float | None, right: float | None) -> float | None:
    return round(left - right, 6) if left is not None and right is not None else None


def _atomic_write_json(destination: Path, payload: Mapping[str, Any]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_path = tempfile.mkstemp(prefix=f".{destination.name}.", dir=str(destination.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
    except BaseException:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass
        raise

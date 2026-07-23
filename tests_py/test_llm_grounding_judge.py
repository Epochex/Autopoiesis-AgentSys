from __future__ import annotations

import json
from collections.abc import Mapping

import pytest
from pydantic import ValidationError

from core.eval.llm_grounding_judge import (
    CandidateOutput,
    EvaluationInputError,
    EvidenceExcerpt,
    FileJudgeCache,
    JudgeRunError,
    LLMJsonJudgeBackend,
    OutputConclusion,
    PairedJudgeCase,
    SafeguardProfile,
    build_withheld_evidence_negative,
    run_paired_llm_judge,
    write_paired_judge_report,
)


FULL = SafeguardProfile(
    evidence_constraint=True,
    citation_verifier=True,
    contract_verifier=True,
    semantic_review=True,
    refusal_gate=True,
)
BASELINE = SafeguardProfile(
    evidence_constraint=False,
    citation_verifier=False,
    contract_verifier=False,
    semantic_review=False,
    refusal_gate=False,
)


class DeterministicSemanticJudge:
    def __init__(self, *, secret: str = "never-serialize-this"):
        self.secret = secret
        self.calls = 0

    @property
    def fingerprint_components(self) -> Mapping[str, str]:
        return {
            "provider_id": "deterministic-test-double",
            "model_id": "semantic-rules-v1",
            "prompt_version": "test-v1",
            "response_schema": "JudgeResponse/1",
        }

    def review(self, request_payload: Mapping[str, object]) -> Mapping[str, object]:
        self.calls += 1
        results = []
        for claim in request_payload["claims"]:  # type: ignore[index]
            evidence = " ".join(item["raw_text"] for item in claim["citations"])
            if "物理链路中断" in evidence:
                verdict = "supported"
                reason = "引用原文直接报告物理链路中断。"
            elif "接口正常" in evidence:
                verdict = "unsupported"
                reason = "引用原文报告接口正常，与中断结论矛盾。"
            else:
                verdict = "insufficient"
                reason = "给定引用不足以推出该结论。"
            results.append(
                {"claim_id": claim["claim_id"], "verdict": verdict, "reason": reason}
            )
        return {"schema_version": 1, "claim_verdicts": results}


class MalformedJudge(DeterministicSemanticJudge):
    def review(self, request_payload: Mapping[str, object]) -> Mapping[str, object]:
        return {"schema_version": 1, "claim_verdicts": []}


class DisagreeingJudge(DeterministicSemanticJudge):
    def review(self, request_payload: Mapping[str, object]) -> Mapping[str, object]:
        labels = ["supported", "unsupported", "insufficient"]
        label = labels[self.calls % len(labels)]
        self.calls += 1
        return {
            "schema_version": 1,
            "claim_verdicts": [
                {"claim_id": claim["claim_id"], "verdict": label, "reason": f"vote={label}"}
                for claim in request_payload["claims"]  # type: ignore[index]
            ],
        }


class RecordingJsonClient:
    def __init__(self):
        self.messages = []
        self.schema_name = ""

    def complete_json(self, messages, *, schema_name):
        self.messages = messages
        self.schema_name = schema_name
        payload = json.loads(messages[-1]["content"])
        return {
            "schema_version": 1,
            "claim_verdicts": [
                {
                    "claim_id": claim["claim_id"],
                    "verdict": "supported",
                    "reason": "证据原文直接支持。",
                }
                for claim in payload["claims"]
            ],
        }


def _output(
    variant_id: str,
    safeguards: SafeguardProfile,
    *,
    evidence_id: str | None = None,
    refused: bool = False,
) -> CandidateOutput:
    return CandidateOutput(
        variant_id=variant_id,
        system_fingerprint=f"{variant_id}-runtime-v1",
        safeguards=safeguards,
        refused=refused,
        refusal_reason="关键链路证据缺失" if refused else "",
        conclusions=(
            []
            if refused
            else [
                OutputConclusion(
                    claim_id="root-cause",
                    text="办公网故障根因是 WAN 物理链路中断。",
                    evidence_ids=[evidence_id] if evidence_id else [],
                )
            ]
        ),
    )


def _paired_cases() -> list[PairedJudgeCase]:
    answerable = PairedJudgeCase(
        case_id="heldout-wan-001",
        expected_answerable=True,
        evidence=[
            EvidenceExcerpt(
                evidence_id="ev-down",
                source="fortigate:event-window",
                raw_text="wan1 carrier lost，物理链路中断。",
            ),
            EvidenceExcerpt(
                evidence_id="ev-up",
                source="switch:poll",
                raw_text="lan2 接口正常，未发现丢包。",
            ),
        ],
        outputs=[
            _output("full", FULL, evidence_id="ev-down"),
            _output("baseline", BASELINE, evidence_id="ev-up"),
        ],
    )
    negative = build_withheld_evidence_negative(
        answerable,
        case_id="heldout-wan-001-withheld",
        withheld_evidence_ids=["ev-down"],
        outputs_from_masked_run=[
            _output("full", FULL, refused=True),
            _output("baseline", BASELINE, evidence_id="ev-up"),
        ],
    )
    return [answerable, negative]


def test_paired_judge_scores_grounding_and_withheld_evidence_refusal(tmp_path):
    backend = DeterministicSemanticJudge()
    report = run_paired_llm_judge(
        _paired_cases(),
        backend,
        full_variant_id="full",
        baseline_variant_id="baseline",
        cache=FileJudgeCache(tmp_path / "cache"),
        repeats=3,
    )

    assert report.boundary.annotation_type == "llm_as_judge"
    assert report.boundary.is_human_gold is False
    assert report.withheld_key_evidence_negatives == 1
    assert report.comparison.full.semantic_citation_accuracy == 1.0
    assert report.comparison.full.unsupported_assertion_rate == 0.0
    assert report.comparison.full.correct_refusal_rate == 1.0
    assert report.comparison.baseline.semantic_citation_accuracy == 0.0
    assert report.comparison.baseline.unsupported_assertion_rate == 1.0
    assert report.comparison.baseline.correct_refusal_rate == 0.0
    assert report.comparison.improvement.semantic_citation_accuracy_gain == 1.0
    assert report.comparison.improvement.unsupported_assertion_rate_reduction == 1.0
    assert report.comparison.improvement.correct_refusal_rate_gain == 1.0

    destination = write_paired_judge_report(report, tmp_path / "report.json")
    payload = destination.read_text(encoding="utf-8")
    assert "never-serialize-this" not in payload
    assert json.loads(payload)["boundary"]["is_human_gold"] is False


def test_cache_replays_each_repeat_without_calling_backend_or_storing_secret(tmp_path):
    cache = FileJudgeCache(tmp_path / "cache")
    first = DeterministicSemanticJudge(secret="ds-key-must-not-leak")
    run_paired_llm_judge(
        _paired_cases(), first, full_variant_id="full", baseline_variant_id="baseline", cache=cache
    )
    assert first.calls == 9

    second = DeterministicSemanticJudge(secret="different-secret")
    run_paired_llm_judge(
        _paired_cases(), second, full_variant_id="full", baseline_variant_id="baseline", cache=cache
    )
    assert second.calls == 0
    assert "ds-key-must-not-leak" not in "".join(
        path.read_text(encoding="utf-8") for path in (tmp_path / "cache").glob("*.json")
    )


def test_malformed_judge_response_fails_closed_and_emits_no_report(tmp_path):
    destination = tmp_path / "must-not-exist.json"
    with pytest.raises(JudgeRunError, match="claim ids do not exactly match"):
        report = run_paired_llm_judge(
            _paired_cases(),
            MalformedJudge(),
            full_variant_id="full",
            baseline_variant_id="baseline",
            repeats=1,
        )
        write_paired_judge_report(report, destination)
    assert not destination.exists()


def test_repeat_disagreement_is_failed_closed_as_insufficient():
    report = run_paired_llm_judge(
        _paired_cases(),
        DisagreeingJudge(),
        full_variant_id="full",
        baseline_variant_id="baseline",
        repeats=3,
    )

    judged = [item for item in report.output_reports if item.verdicts]
    assert judged
    assert all(item.verdicts[0].verdict == "insufficient" for item in judged)
    assert all(item.verdicts[0].disputed for item in judged)
    assert report.comparison.full.disputed_count == 1


def test_suite_refuses_to_compute_correct_refusal_without_withheld_negative():
    with pytest.raises(EvaluationInputError, match="withholding key evidence"):
        run_paired_llm_judge(
            _paired_cases()[:1],
            DeterministicSemanticJudge(),
            full_variant_id="full",
            baseline_variant_id="baseline",
        )


def test_case_rejects_citation_without_evidence_raw_text():
    with pytest.raises(ValidationError, match="without raw text"):
        PairedJudgeCase(
            case_id="bad-citation",
            expected_answerable=True,
            evidence=[],
            outputs=[_output("full", FULL, evidence_id="unknown")],
        )


def test_baseline_profile_must_disable_architectural_safeguards():
    cases = _paired_cases()
    partly_enabled = BASELINE.model_copy(update={"evidence_constraint": True})
    cases[0].outputs[1] = cases[0].outputs[1].model_copy(update={"safeguards": partly_enabled})
    cases[1].outputs[1] = cases[1].outputs[1].model_copy(update={"safeguards": partly_enabled})

    with pytest.raises(EvaluationInputError, match="must disable evidence constraint"):
        run_paired_llm_judge(
            cases,
            DeterministicSemanticJudge(),
            full_variant_id="full",
            baseline_variant_id="baseline",
        )


def test_llm_backend_receives_claim_raw_evidence_and_expected_answerability():
    client = RecordingJsonClient()
    backend = LLMJsonJudgeBackend(
        client,
        provider_id="deepseek",
        model_id="deepseek-pro",
        prompt_version="judge-v-test",
    )
    request_payload = {
        "schema_version": 1,
        "evaluation_task": "semantic_claim_evidence_support",
        "case_id": "heldout-raw",
        "expected_answerable": False,
        "claims": [
            {
                "claim_id": "claim-1",
                "text": "链路中断",
                "citations": [
                    {
                        "evidence_id": "ev-1",
                        "source": "raw-log",
                        "raw_text": "wan1 carrier lost",
                    }
                ],
            }
        ],
        "allowed_verdicts": ["supported", "unsupported", "insufficient"],
    }

    response = backend.review(request_payload)

    submitted = json.loads(client.messages[-1]["content"])
    assert client.schema_name == "JudgeResponse_v1"
    assert submitted["expected_answerable"] is False
    assert submitted["claims"][0]["text"] == "链路中断"
    assert submitted["claims"][0]["citations"][0]["raw_text"] == "wan1 carrier lost"
    assert response["claim_verdicts"][0]["verdict"] == "supported"

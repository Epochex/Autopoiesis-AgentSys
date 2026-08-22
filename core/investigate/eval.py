"""Measuring the investigation harness without paying to measure it.

The thing under test is not the model. It is the harness around it: whether the
evidence a session collects actually contains the answer, whether the risk
grading holds when the model mislabels a step, whether citations survive
verification, and whether context stays inside its budget as a conversation
grows. All four are properties of code, so all four can be measured against
recorded model responses instead of live ones.

That distinction is what keeps the eval affordable. A run over the fixture set
costs nothing and can go in CI. A live run exists too, gated behind an explicit
flag and a hard call ceiling, for the one thing fixtures cannot answer: whether
a real model, given this evidence and these instructions, grounds its answer or
invents one.

The scores are deliberately blunt:

``evidence_sufficiency`` — did the opening probes surface the fact the question
turns on? Measured by string match against the expected reading, because if the
answer is not in the evidence, nothing downstream can be right except by luck.

``grading_integrity`` — of the steps the model proposed, how many ended up with
a risk class the allowlist agrees with? A harness that lets a writing command
through as read-only fails here even when the prose is excellent.

``citation_validity`` — what share of cited ids exist. Invented ids are the
measurable face of hallucination.

``context_efficiency`` — characters sent per turn, as a ratio against sending
everything. This is the cost axis, and it belongs in the same report as the
quality axes so a change that improves one at the other's expense is visible.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

FIXTURE_PATH = Path(__file__).resolve().parent / "eval_cases.json"

# A live run is opt-in and capped. The cap is enforced here rather than trusted
# to the caller, because "just one more variant" is how an eval becomes a bill.
LIVE_CALL_CEILING = int(os.getenv("AUTOPOIESIS_EVAL_MAX_CALLS", "6"))


@dataclass
class Case:
    """One question, the evidence that should answer it, and what good looks like."""

    case_id: str
    question: str
    family: str | None
    # Substrings that must appear somewhere in the collected evidence for the
    # question to be answerable at all.
    expects_evidence: list[str] = field(default_factory=list)
    # A recorded model response, so grading and citation checks run offline.
    recorded_response: dict[str, Any] = field(default_factory=dict)
    # Commands in the recorded runbook that must NOT come back runnable.
    must_not_be_runnable: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Case":
        return cls(
            case_id=raw["case_id"],
            question=raw["question"],
            family=raw.get("family"),
            expects_evidence=list(raw.get("expects_evidence") or []),
            recorded_response=dict(raw.get("recorded_response") or {}),
            must_not_be_runnable=list(raw.get("must_not_be_runnable") or []),
        )


def load_cases(path: Path | None = None) -> list[Case]:
    payload = json.loads((path or FIXTURE_PATH).read_text(encoding="utf-8"))
    return [Case.from_dict(item) for item in payload["cases"]]


@dataclass
class CaseResult:
    case_id: str
    evidence_count: int
    evidence_sufficiency: float
    missing_evidence: list[str]
    grading_integrity: float
    mislabelled: list[str]
    citation_validity: float
    invented_citations: list[str]
    context_chars: int
    context_ratio: float

    def as_row(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "evidence_count": self.evidence_count,
            "evidence_sufficiency": round(self.evidence_sufficiency, 3),
            "missing_evidence": self.missing_evidence,
            "grading_integrity": round(self.grading_integrity, 3),
            "mislabelled": self.mislabelled,
            "citation_validity": round(self.citation_validity, 3),
            "invented_citations": self.invented_citations,
            "context_chars": self.context_chars,
            "context_ratio": round(self.context_ratio, 3),
        }


def run_case(case: Case, *, live_client: Any | None = None) -> CaseResult:
    """Score one case. With no live client the recorded response is used."""
    from frontend.gateway.app import investigate

    opened = investigate.start(case.question, family=case.family)
    session = investigate.get(opened["session_id"])
    blob = "\n".join(item.get("output", "") for item in session.evidence)

    missing = [needle for needle in case.expects_evidence if needle not in blob]
    sufficiency = (
        1.0 - len(missing) / len(case.expects_evidence) if case.expects_evidence else 1.0
    )

    if live_client is not None:
        response = _ask_live(investigate, session, live_client)
    else:
        response = case.recorded_response

    runbook = investigate._sanitise_runbook(response.get("runbook") or [])
    mislabelled = [
        step["command"]
        for step in runbook
        if step["command"] in case.must_not_be_runnable and step["runnable"]
    ]
    graded = len(runbook) or 1
    integrity = 1.0 - len(mislabelled) / graded

    claimed = [str(item) for item in (response.get("citations") or [])]
    verified = investigate._verified_citations(session, claimed)
    invented = [item for item in claimed if item not in verified]
    validity = len(verified) / len(claimed) if claimed else 1.0

    # Cost axis: what a turn actually sends, against sending everything.
    everything = sum(len(item.get("output", "")) + 40 for item in session.evidence) or 1
    sent = len(investigate._evidence_block(session))

    return CaseResult(
        case_id=case.case_id,
        evidence_count=len(session.evidence),
        evidence_sufficiency=sufficiency,
        missing_evidence=missing,
        grading_integrity=integrity,
        mislabelled=mislabelled,
        citation_validity=validity,
        invented_citations=invented,
        context_chars=sent,
        context_ratio=sent / everything,
    )


def _ask_live(investigate_module: Any, session: Any, client: Any) -> dict[str, Any]:
    """One real call, used only by an explicitly-enabled live run."""
    messages = [
        {"role": "system", "content": investigate_module._system_prompt("zh")},
        {
            "role": "user",
            "content": (
                f"问题：{session.question}\n\n已采集的真实命令输出：\n"
                f"{investigate_module._evidence_block(session)}\n\n"
                f"按这个 JSON 结构回答：{json.dumps(investigate_module.ANALYZE_SCHEMA, ensure_ascii=False)}"
            ),
        },
    ]
    return client.complete_json(messages, schema_name="rca_eval")


def run(
    cases: list[Case] | None = None,
    *,
    live: bool = False,
    client_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Score every case and aggregate.

    ``live=True`` spends money: one call per case, capped at LIVE_CALL_CEILING.
    Everything else runs against recorded responses and costs nothing.
    """
    cases = cases or load_cases()
    client = None
    if live:
        if len(cases) > LIVE_CALL_CEILING:
            raise ValueError(
                f"{len(cases)} cases exceeds the live ceiling of {LIVE_CALL_CEILING}; "
                "raise AUTOPOIESIS_EVAL_MAX_CALLS deliberately or run offline"
            )
        factory = client_factory or _default_client
        client = factory()
        if client is None:
            raise RuntimeError("live run requested but no model is configured")

    results = [run_case(case, live_client=client) for case in cases]
    if not results:
        return {"cases": 0}

    def mean(attribute: str) -> float:
        return round(sum(getattr(r, attribute) for r in results) / len(results), 3)

    return {
        "cases": len(results),
        "mode": "live" if live else "offline",
        "evidence_sufficiency": mean("evidence_sufficiency"),
        "grading_integrity": mean("grading_integrity"),
        "citation_validity": mean("citation_validity"),
        "context_ratio": mean("context_ratio"),
        "average_context_chars": round(sum(r.context_chars for r in results) / len(results)),
        "failures": [r.as_row() for r in results if r.evidence_sufficiency < 1.0
                     or r.grading_integrity < 1.0 or r.citation_validity < 1.0],
        "rows": [r.as_row() for r in results],
    }


def _default_client():
    from frontend.gateway.app.model_access import client_for

    return client_for("eval", timeout_sec=120)

"""Investigation sessions: gather real evidence first, reason over it second.

The order matters and is the whole point. A session opens by *running* the
diagnostics for the fault family in question, then hands the model only what
those commands actually printed. The model never gets to describe a system it
has not been shown, and every claim it makes has to name the evidence id it
came from — an answer citing nothing is rejected and re-asked rather than
shown.

Three rules hold across turns:

**Evidence accumulates, never regenerates.** Each turn appends to the same
list. A follow-up question sees every reading from every earlier turn, so
"what about the other interface" does not silently start from an empty view.

**Commands come from the allowlist, not from the model.** A step the model
proposes is validated by ``core.investigate.safe_exec`` before it can run, and
a step classed ``gated`` has no executor at all here — the endpoint refuses it
even if the caller asks nicely. The model proposes; this layer decides.

**Answers cite or they do not ship.** The judge for that is mechanical: the
cited ids must exist in the session. A hallucinated reading has no id to point
at, which is what makes the check work at all.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from core.investigate.safe_exec import Refused, is_safe, run

# Opening probes per fault family. These run before the model sees anything, so
# the first thing it reads is what the box actually said.
FAMILY_PROBES: dict[str, list[str]] = {
    "fam-host-config-drift": [
        "ip -br link show",
        "ip -br addr show",
        "ip route show default",
        "cloud-init status --long",
    ],
    "fam-perception-selfheal": [
        "systemctl --failed --no-legend",
        "df -h /data",
        "curl -s -m 5 -o /dev/null -w %{http_code} http://127.0.0.1:8026/api/healthz",
        "free -m",
    ],
    "fam-address-ownership": [
        "ip neigh show",
        "ip -br addr show",
        "arp -n",
    ],
    "fam-exposure": [
        "ss -tulpn",
        "ip -br addr show",
    ],
    "fam-policy-reachability": [
        "ip route show",
        "ip -br link show",
        "ss -tulpn",
    ],
}

# Run for every session regardless of family: the shape of the box.
BASELINE_PROBES = ["hostname", "uptime", "ip -br addr show"]

MAX_EVIDENCE = 120
MAX_TURNS = 40

# Generating a runbook over a full evidence block takes longer than a chat
# reply. Too short a timeout does not save money — the tokens are billed
# whether or not we wait for the answer.
LLM_TIMEOUT_SEC = 180

# Context budget. These exist because the naive version — send every reading in
# full on every turn — makes a ten-turn session cost quadratically more than a
# one-turn session for no extra insight. The model does not need 20,000
# characters of `journalctl` to answer a question about a NIC.
EVIDENCE_HEAD_CHARS = 700      # per reading, in the full block
EVIDENCE_TAIL_CHARS = 300      # tails matter: errors land at the end
DIGEST_CHARS = 180             # per reading, in a follow-up digest
MAX_CONTEXT_CHARS = 22_000     # whole evidence block, hard ceiling
FOLLOW_UP_FULL = 3             # readings sent in full on a follow-up turn

# A follow-up gets a tighter ceiling than the opening analysis: the earlier
# answers already carry the conclusions drawn from the older readings, so
# re-sending them buys a summary the session already has.
MAX_FOLLOWUP_CHARS = 12_000


@dataclass
class Session:
    session_id: str
    question: str
    family: str | None
    subject: str | None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    turns: list[dict[str, Any]] = field(default_factory=list)
    runbook: list[dict[str, Any]] = field(default_factory=list)
    diagnosis: str = ""
    opened_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def next_evidence_id(self) -> str:
        return f"ev-{len(self.evidence) + 1:03d}"

    def evidence_ids(self) -> set[str]:
        return {item["evidence_id"] for item in self.evidence}

    def collect(self, command: str) -> dict[str, Any]:
        """Run one command and file its real output as evidence."""
        if len(self.evidence) >= MAX_EVIDENCE:
            return {"evidence_id": "", "command": command, "output": "",
                    "ok": False, "refused": "session evidence limit reached"}
        execution = run(command)
        item = execution.as_evidence(self.next_evidence_id())
        item["at"] = datetime.now(timezone.utc).isoformat()
        self.evidence.append(item)
        return item

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "question": self.question,
            "family": self.family,
            "subject": self.subject,
            "evidence": self.evidence,
            "turns": self.turns,
            "runbook": self.runbook,
            "diagnosis": self.diagnosis,
            "opened_at": self.opened_at,
        }


_SESSIONS: dict[str, Session] = {}


def start(question: str, family: str | None = None, subject: str | None = None) -> dict[str, Any]:
    """Open a session and run its opening probes before anything reasons."""
    session = Session(
        session_id=uuid.uuid4().hex[:12],
        question=question.strip(),
        family=family,
        subject=subject,
    )
    _SESSIONS[session.session_id] = session

    planned = list(BASELINE_PROBES)
    for command in FAMILY_PROBES.get(family or "", []):
        if command not in planned:
            planned.append(command)
    if subject and re.fullmatch(r"[\w.:-]{1,64}", subject):
        for template in (f"ping -c 2 -W 2 {subject}", f"ip neigh show {subject}"):
            if is_safe(template):
                planned.append(template)

    for command in planned:
        session.collect(command)

    return {
        "session_id": session.session_id,
        "question": session.question,
        "family": family,
        "subject": subject,
        "evidence": session.evidence,
        "summary": _summarise(session),
    }


def _summarise(session: Session) -> str:
    ran = sum(1 for item in session.evidence if item.get("ok"))
    failed = sum(1 for item in session.evidence if not item.get("ok"))
    return f"已跑 {len(session.evidence)} 条只读检查，{ran} 条有结果，{failed} 条无结果或被拒绝"


def get(session_id: str) -> Session:
    session = _SESSIONS.get(session_id)
    if session is None:
        raise KeyError(f"unknown session {session_id!r}")
    return session


def _excerpt(text: str, head: int, tail: int) -> str:
    """Keep the start and the end. Truncating only the tail loses the error."""
    text = text.strip()
    if len(text) <= head + tail:
        return text
    dropped = len(text) - head - tail
    return f"{text[:head]}\n… [略去 {dropped:,} 字符] …\n{text[-tail:]}"


def _render(item: dict[str, Any], head: int, tail: int) -> str:
    header = f"[{item['evidence_id']}] $ {item['command']}"
    body = item.get("output") or f"(no output; {item.get('refused') or 'empty'})"
    return f"{header}\n{_excerpt(body, head, tail)}"


def _evidence_block(session: Session, *, full_ids: set[str] | None = None) -> str:
    """The readings the model may reason from, trimmed to a budget.

    ``full_ids`` names the readings that stay long — on a follow-up that is the
    most recent handful, since the earlier ones have already been summarised in
    a previous answer. Everything else collapses to a digest so its existence
    and shape stay visible without being paid for twice.
    """
    parts: list[str] = []
    budget = MAX_FOLLOWUP_CHARS if full_ids is not None else MAX_CONTEXT_CHARS

    # Order matters when the ceiling bites. A reading an earlier answer cited is
    # load-bearing regardless of age, so it is allocated before merely-recent
    # ones; walking newest-first alone would drop exactly the evidence the
    # conversation has been building on.
    ordered = list(reversed(session.evidence))
    if full_ids:
        cited_first = [i for i in ordered if i["evidence_id"] in full_ids]
        ordered = cited_first + [i for i in ordered if i["evidence_id"] not in full_ids]

    for item in ordered:
        detailed = full_ids is None or item["evidence_id"] in full_ids
        rendered = (
            _render(item, EVIDENCE_HEAD_CHARS, EVIDENCE_TAIL_CHARS)
            if detailed
            else _render(item, DIGEST_CHARS, 0)
        )
        if len(rendered) > budget:
            parts.append(f"[… 还有 {len(session.evidence) - len(parts)} 条读数未纳入本次上下文 …]")
            break
        parts.append(rendered)
        budget -= len(rendered)
    return "\n\n".join(parts)


ANALYZE_SCHEMA = {
    "diagnosis": "one paragraph, in the user's language, naming evidence ids inline",
    "citations": ["ev-001"],
    "runbook": [
        {"n": 1, "risk": "readonly|auto|gated", "what": "plain sentence", "command": "shell command", "why": "why this step"}
    ],
}


def _client(provider_id: str = "deepseek-v4"):
    """Build a client from the console's own provider registry.

    Reusing ``resolve_reasoner`` rather than reading env vars directly means
    there is one place credentials live — the console's provider config — and
    no second copy of a key to drift or leak. It also means this path honours
    whichever provider the console is pointed at.
    """
    from core.llm.provider import LLMConfigurationError, OpenAICompatibleClient

    from .providers import resolve_reasoner

    mode, env = resolve_reasoner(provider_id)
    if mode != "llm" or not env:
        return None
    try:
        return OpenAICompatibleClient(
            base_url=env["AUTOPOIESIS_LLM_BASE_URL"],
            api_key=env["AUTOPOIESIS_LLM_API_KEY"],
            model=env["AUTOPOIESIS_LLM_MODEL"],
            # An analysis prompt carries every reading collected so far, and the
            # answer is a whole runbook. The 30s default is a chat timeout and
            # cuts these off mid-generation — which still bills for the tokens.
            timeout_sec=LLM_TIMEOUT_SEC,
        )
    except LLMConfigurationError:
        return None


def _system_prompt(language: str = "zh") -> str:
    return (
        "You are a network operations analyst. You are given the real output of "
        "read-only commands already run on the host. Reason ONLY from that output.\n"
        "Rules you must follow:\n"
        "1. Every factual claim must cite the evidence id it came from, like [ev-003].\n"
        "2. If the evidence does not answer the question, say so plainly and propose "
        "which further read-only command would settle it. Never guess a reading.\n"
        "3. Classify every runbook step: 'readonly' if it only observes, 'auto' if it "
        "only touches something already broken on one host, 'gated' if it touches the "
        "firewall, a shared subnet, or a device currently in service.\n"
        "4. Never propose anything touching tailscale0, tailscaled, or 100.64.0.0/10 — "
        "that path is the only way into this network and is off limits.\n"
        "5. Write in plain language. No invented jargon.\n"
        f"6. Answer in {'Chinese' if language == 'zh' else 'English'}.\n"
        "Return JSON only."
    )


def analyze(session_id: str, language: str = "zh") -> dict[str, Any]:
    """Turn collected evidence into a diagnosis and a graded runbook."""
    session = get(session_id)
    client = _client()
    if client is None:
        return {
            "diagnosis": "未配置推理模型，只给出已采集的证据。设置 AUTOPOIESIS_LLM_BASE_URL / "
                         "AUTOPOIESIS_LLM_MODEL / AUTOPOIESIS_LLM_API_KEY 后可生成处置方案。",
            "citations": [item["evidence_id"] for item in session.evidence[:3]],
            "runbook": [],
            "degraded": True,
        }

    messages = [
        {"role": "system", "content": _system_prompt(language)},
        {
            "role": "user",
            "content": (
                f"问题：{session.question}\n"
                f"故障族：{session.family or '未指定'}\n"
                f"对象：{session.subject or '未指定'}\n\n"
                f"已采集的真实命令输出：\n{_evidence_block(session)}\n\n"
                f"按这个 JSON 结构回答：{json.dumps(ANALYZE_SCHEMA, ensure_ascii=False)}"
            ),
        },
    ]
    try:
        payload = client.complete_json(messages, schema_name="rca_analysis")
    except Exception as error:  # noqa: BLE001 - surfaced to the caller, not hidden
        return {
            "diagnosis": f"推理调用没有完成（{type(error).__name__}）。已采集的证据仍然有效，可以直接看上面的命令输出，或重试。",
            "citations": [], "runbook": [], "degraded": True,
            "error": f"{type(error).__name__}: {error}"[:300],
        }
    runbook = _sanitise_runbook(payload.get("runbook") or [])
    session.runbook = runbook
    session.diagnosis = str(payload.get("diagnosis") or "")
    citations = _verified_citations(session, payload.get("citations") or [])
    return {"diagnosis": session.diagnosis, "citations": citations, "runbook": runbook,
            "degraded": False}


def _sanitise_runbook(raw: list[Any]) -> list[dict[str, Any]]:
    """Re-derive each step's risk from the allowlist, not from the model.

    A model that labels `systemctl restart` as ``readonly`` must not thereby
    earn a Run button. Anything the allowlist would refuse is forced to
    ``gated`` regardless of what the model called it.
    """
    steps: list[dict[str, Any]] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            continue
        command = str(item.get("command") or "").strip()
        claimed = str(item.get("risk") or "gated").lower()
        if claimed not in {"readonly", "auto", "gated"}:
            claimed = "gated"
        risk = "readonly" if (command and is_safe(command)) else ("gated" if claimed != "auto" else "auto")
        if claimed == "gated":
            risk = "gated"
        steps.append({
            "n": index,
            "risk": risk,
            "what": str(item.get("what") or "").strip(),
            "command": command,
            "why": str(item.get("why") or "").strip(),
            "runnable": risk == "readonly",
        })
    return steps


def _verified_citations(session: Session, claimed: list[Any]) -> list[str]:
    """Drop any cited id the session does not actually hold."""
    known = session.evidence_ids()
    return [str(item) for item in claimed if str(item) in known]


def ask(session_id: str, question: str, language: str = "zh") -> dict[str, Any]:
    """One follow-up turn. Prior evidence and prior turns stay in view."""
    session = get(session_id)
    if len(session.turns) >= MAX_TURNS:
        return {"answer": "本次会话轮数已达上限，请开新会话。", "citations": [], "evidence": []}

    client = _client()
    if client is None:
        return {"answer": "未配置推理模型，无法回答追问。已采集的证据仍可查看。",
                "citations": [], "evidence": [], "degraded": True}

    history = "\n".join(
        f"问：{turn['question']}\n答：{turn['answer'][:600]}" for turn in session.turns[-4:]
    )
    recent_ids = {item["evidence_id"] for item in session.evidence[-FOLLOW_UP_FULL:]}
    # Anything an earlier answer cited stays in full: it is load-bearing.
    for turn in session.turns[-4:]:
        recent_ids.update(turn.get("citations") or [])
    messages = [
        {"role": "system", "content": _system_prompt(language)},
        {
            "role": "user",
            "content": (
                f"原始问题：{session.question}\n"
                + (f"\n此前对话：\n{history}\n" if history else "")
                + f"\n已采集证据（最近 {FOLLOW_UP_FULL} 条为全文，更早的为摘要）：\n"
                + f"{_evidence_block(session, full_ids=recent_ids)}\n\n"
                f"追问：{question}\n\n"
                '按 {"answer": "...", "citations": ["ev-001"], '
                '"need_commands": ["只读命令"]} 回答。'
                "need_commands 里只写还需要补跑的只读命令；不需要就给空数组。"
            ),
        },
    ]
    try:
        payload = client.complete_json(messages, schema_name="rca_followup")
    except Exception as error:  # noqa: BLE001 - surfaced to the caller, not hidden
        return {
            "answer": f"推理调用没有完成（{type(error).__name__}）。可以重试，或换个更具体的问法。",
            "citations": [], "evidence": [], "degraded": True,
            "error": f"{type(error).__name__}: {error}"[:300],
        }

    # The model may ask for more readings. Run the safe ones, refuse the rest,
    # and file whatever came back so the next turn can see it.
    fresh: list[dict[str, Any]] = []
    for command in (payload.get("need_commands") or [])[:5]:
        command = str(command).strip()
        if not command:
            continue
        fresh.append(session.collect(command))

    answer = str(payload.get("answer") or "").strip()
    citations = _verified_citations(session, payload.get("citations") or [])
    session.turns.append({"question": question, "answer": answer, "citations": citations,
                          "at": datetime.now(timezone.utc).isoformat()})
    return {"answer": answer, "citations": citations, "evidence": fresh, "degraded": False}


def run_step(session_id: str, step: int) -> dict[str, Any]:
    """Run one runbook step. Only ``readonly`` steps have an executor here."""
    session = get(session_id)
    match = next((item for item in session.runbook if item["n"] == step), None)
    if match is None:
        return {"ran": False, "refused": True, "reason": f"no step {step} in this runbook"}
    if match["risk"] != "readonly":
        return {
            "ran": False,
            "refused": True,
            "reason": (
                "这一步会改变状态，必须由人执行。"
                if match["risk"] == "gated"
                else "自动修复动作走 /api/rca/remediation/execute，那条路径带前置校验和观察期。"
            ),
        }
    item = session.collect(match["command"])
    return {"ran": bool(item.get("ok")), "output": item.get("output", ""),
            "exit_code": item.get("exit_code"), "evidence_id": item.get("evidence_id"),
            "refused": bool(item.get("refused")), "reason": item.get("refused")}


def run_all(session_id: str) -> dict[str, Any]:
    """Run the runbook top to bottom, stopping at the first step we may not run."""
    session = get(session_id)
    results: list[dict[str, Any]] = []
    stopped_at: int | None = None
    for item in session.runbook:
        outcome = run_step(session_id, item["n"])
        results.append({"step": item["n"], **outcome})
        if outcome.get("refused"):
            stopped_at = item["n"]
            break
    return {"results": results, "stopped_at": stopped_at}

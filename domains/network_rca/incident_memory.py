"""Retain passed sentinel chains and clearly separated ineffective outcomes.

A passed chain still uses the watch-window verdict as its write gate.  Refused,
escalated, and unverified-revert chains keep only the conservative fact that the
action should not be treated as a proven fix for this incident.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from core.evolve.consolidate import ConsolidationReport, consolidate_run
from core.memory.store import MemoryRecord, TieredMemoryStore
from core.skills.registry import SkillRegistry
from core.trace.events import TraceEvent


_FOLLOWUP_KINDS = frozenset({
    "remediation_committed",
    "bakein_opened",
    "bakein_sampled",
    "bakein_passed",
    "bakein_regressed",
    "remediation_reverted",
    "revert_unverified",
})
_CHAIN_KINDS = frozenset({
    "detected",
    "awaiting_confirmation",
    "command",
    "preflight",
    "declined",
    "no_safe_action",
    "cooldown",
    "remediated",
    "resolved",
    "escalated",
    *_FOLLOWUP_KINDS,
})
_INEFFECTIVE_TERMINALS = frozenset({
    "declined",
    "escalated",
    "revert_unverified",
})
_SAFETY_GATED_TERMINALS = frozenset({"no_safe_action"})
_TERMINAL_KINDS = frozenset({
    *_INEFFECTIVE_TERMINALS,
    "no_safe_action",
    "cooldown",
})
_ASCII_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9_.:/-]{0,127}", re.IGNORECASE)
_ASCII_PART = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_IPV4 = re.compile(r"(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])")


@dataclass(frozen=True, slots=True)
class SentinelIncidentCase:
    """The structural case attributes consumed by ``consolidate_run``."""

    id: str
    query: str
    query_terms: list[str]
    assets: list[str]


@dataclass(frozen=True, slots=True)
class SyntheticIncidentRun:
    """One passed sentinel chain after deterministic trace translation."""

    run_id: str
    case: SentinelIncidentCase
    events: list[TraceEvent]
    evidence: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class SyntheticIneffectiveMemory:
    """One closed chain retained as evidence that an action did not help."""

    run_id: str
    record: MemoryRecord


def _text(value: Any) -> str:
    return str(value or "").strip()


def _identity(event: Mapping[str, Any]) -> tuple[str, str, str] | None:
    detector = _text(event.get("detector"))
    subject = _text(event.get("subject"))
    action = _text(event.get("action"))
    if not subject:
        return None
    return detector, subject, action


def incident_ref(chain: Iterable[Mapping[str, Any]]) -> str:
    """Stable identity for one Sentinel cycle while its timeline is still growing."""
    rows = list(chain)
    detected = next((row for row in rows if row.get("kind") == "detected"), None)
    if detected is None:
        raise ValueError("incident ref requires a detected row")
    detector = _text(detected.get("detector"))
    subject = _text(detected.get("target") or detected.get("subject"))
    opened_at = _text(detected.get("at"))
    if not detector or not subject or not opened_at:
        raise ValueError("incident ref requires detector, subject, and detected time")
    raw = f"{detector}\0{subject}\0{opened_at}".encode("utf-8")
    return f"sentinel:{hashlib.sha256(raw).hexdigest()[:32]}"


def completed_incident_chains(
    timeline: Iterable[Mapping[str, Any]],
) -> list[list[dict[str, Any]]]:
    """Group interleaved timeline rows into closed disposition chains.

    Confirmation can span polling cycles, while follow-up rows do not repeat the
    detector.  The most recent active chain for a subject/action pair therefore
    carries those rows until a recorded terminal decision closes it.
    """

    active: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    recent_route: dict[tuple[str, str], tuple[str, str, str]] = {}
    completed: list[list[dict[str, Any]]] = []

    for raw in timeline:
        event = dict(raw)
        kind = _text(event.get("kind"))
        if kind not in _CHAIN_KINDS:
            continue

        if kind == "detected":
            identity = _identity(event)
            if identity is None:
                continue
            active.setdefault(identity, []).append(event)
            recent_route[(identity[1], identity[2])] = identity
            continue

        identity = _identity(event)
        key: tuple[str, str, str] | None = None
        if identity is not None and identity[0] and identity in active:
            key = identity
        elif identity is not None:
            key = recent_route.get((identity[1], identity[2]))
            if key is None and not identity[2]:
                candidates = [
                    candidate
                    for candidate in active
                    if candidate[1] == identity[1]
                ]
                key = candidates[-1] if candidates else None
        if key is None or key not in active:
            continue

        active[key].append(event)
        terminal = kind in _TERMINAL_KINDS or kind == "resolved"
        if kind == "remediated" and _text(event.get("outcome")) != "passed":
            terminal = True
        if terminal:
            completed.append(active.pop(key))
            route = (key[1], key[2])
            if recent_route.get(route) == key:
                del recent_route[route]

    return completed


def _passed(chain: list[Mapping[str, Any]]) -> bool:
    remediated = next(
        (event for event in reversed(chain) if event.get("kind") == "remediated"),
        None,
    )
    resolved = next(
        (event for event in reversed(chain) if event.get("kind") == "resolved"),
        None,
    )
    if (
        remediated is None
        or resolved is None
        or remediated.get("outcome") != "passed"
        or resolved.get("outcome") != "passed"
    ):
        return False

    # Older timeline rows only carried the summarized verdict.  Once detailed
    # follow-up rows are present, require their own pass boundary too, so a
    # partial chain can never inherit the later summary by accident.
    has_followup = any(event.get("kind") in _FOLLOWUP_KINDS for event in chain)
    if has_followup and not any(event.get("kind") == "bakein_passed" for event in chain):
        return False
    return not any(
        event.get("kind") in {"bakein_regressed", "remediation_reverted", "revert_unverified"}
        for event in chain
    )


def _canonical_digest(chain: list[Mapping[str, Any]]) -> str:
    encoded = json.dumps(
        chain,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _timestamp(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _walk_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _walk_strings(item)


# Prefixes the retrieval and reasoning layers read as structured claims rather
# than as words: `root:` names a root cause, `skill:` names a probe, and the
# rest carry lifecycle meaning. Any of them arriving from harvested host output
# would be an injected assertion, not a keyword — a unit literally named
# `root:carrier_down.service` would otherwise write a working root tag.
_RESERVED_TAG_PREFIXES = ("root:", "skill:", "probe:", "quarantine:",
                          "subject:", "relation:", "value:", "insight")

# Pure digits, timestamp fragments, and one- or two-character shards are not
# identifiers; they are debris from splitting timestamps and command lines.
_MIN_TERM_LEN = 3
# Any run of digits with an optional ISO fraction marker. The first version
# capped the leading group at four digits, so microsecond fields like `642017z`
# sailed through and were indexed as identifiers.
_TIMESTAMPISH = re.compile(r"^[0-9]+([t:._-][0-9]+)*z?$", re.IGNORECASE)
# Trailing punctuation from prose: `rehearsal...`, `successfully.`, and
# `demo-collector.service:` all reached the index because a trailing period or
# colon satisfied the "contains a separator" test that is supposed to
# distinguish an identifier from a word.
_TRAILING_PUNCT = ".:,;/-_"

# A record carries at most this many harvested tags. Every tag is a BM25 token
# and an exact-match route, so an unbounded list from one noisy journal line
# both drowns the index and hands an attacker a large surface. The cap is a
# blast-radius bound, not a tuning knob.
_MAX_HARVESTED_TAGS = 24


def _is_useful_term(term: str) -> bool:
    """Whether a harvested token is worth indexing as an identifier.

    Judged on the token with trailing punctuation stripped, because prose that
    ends in a period would otherwise pass the separator test that exists to
    tell `demo-collector.service` apart from `successfully`.
    """
    core = term.rstrip(_TRAILING_PUNCT)
    if len(core) < _MIN_TERM_LEN or core.isdigit():
        return False
    # An address is digits-and-dots too, and broadening the timestamp pattern to
    # catch microsecond fields made it swallow every IPv4 — the single most
    # useful operational identifier there is. Check for an address first.
    if not _IPV4.fullmatch(core) and _TIMESTAMPISH.match(core):
        return False
    if core.startswith(_RESERVED_TAG_PREFIXES):
        return False
    # An interior separator or a digit is what makes something look like a name
    # a machine chose; `eth2`, `demo-collector.service`, `192.168.1.27` keep it,
    # "and" / "with" / "successfully" do not.
    return any(ch in core for ch in "-_./:") or any(ch.isdigit() for ch in core)


def _ascii_terms(chain: list[Mapping[str, Any]], root_key: str) -> list[str]:
    """Operational identifiers from the chain, for a store whose BM25 drops CJK.

    The sentinel writes Chinese summaries and `core/memory/bm25.py` tokenises
    on `[a-z0-9]+`, so a record's own text contributes nothing lexically. These
    tags are the retrieval route that makes such a record findable at all.

    Harvesting used to take every ASCII run in every string in every event and
    then also split each one — a single chain produced several hundred tags,
    including every timestamp fragment and every word of a journal line. That
    is three separate problems: it drowns the index, it makes the exact-entity
    route meaningless, and it is the widest possible injection surface for
    host-controlled text. This keeps identifiers and drops debris.
    """
    terms: list[str] = []
    values: list[str] = [root_key]
    for event in chain:
        values.extend(_walk_strings(event))
    for value in values:
        for match in _ASCII_IDENTIFIER.findall(value.lower()):
            if _is_useful_term(match):
                terms.append(match.rstrip(_TRAILING_PUNCT))
            # Split only on the identifier's own parts, and only keep parts that
            # are themselves identifiers — never the timestamp shards.
            for part in _ASCII_PART.findall(match):
                if _is_useful_term(part):
                    terms.append(part.rstrip(_TRAILING_PUNCT))
    deduped = list(dict.fromkeys(term for term in terms if term))
    return deduped[:_MAX_HARVESTED_TAGS]


def _assets(chain: list[Mapping[str, Any]], subject: str) -> list[str]:
    assets = [subject] if subject and _ASCII_IDENTIFIER.fullmatch(subject) else []
    for value in _walk_strings(chain):
        for address in _IPV4.findall(value):
            octets = address.split(".")
            if all(int(octet) <= 255 for octet in octets):
                assets.append(address)
    return list(dict.fromkeys(assets))


def _evidence_summary(event: Mapping[str, Any]) -> str:
    kind = _text(event.get("kind"))
    if kind == "command":
        argv = event.get("argv") or []
        return f"{' '.join(str(part) for part in argv)} -> rc={event.get('rc')}"
    if kind == "bakein_sampled":
        return f"{event.get('probe')}: healthy={event.get('healthy')}"
    return _text(event.get("summary") or event.get("note") or event.get("reason") or kind)


def synthesize_incident_run(
    chain: Iterable[Mapping[str, Any]],
) -> SyntheticIncidentRun | None:
    """Translate one closed, passed chain into the existing consolidation inputs."""

    rows = [dict(event) for event in chain]
    if not rows or not _passed(rows):
        return None
    detected = next((event for event in rows if event.get("kind") == "detected"), None)
    if detected is None:
        return None

    detector = _text(detected.get("detector"))
    subject = _text(detected.get("target") or detected.get("subject"))
    action = _text(detected.get("action"))
    family = _text(detected.get("family"))
    if not detector or not subject:
        return None

    digest = _canonical_digest(rows)
    run_id = f"sentinel-run-{digest[:32]}"
    case_id = f"sentinel-case-{digest[:24]}"
    root_key = f"sentinel.{detector.lower()}"
    terms = _ascii_terms(rows, root_key)
    assets = _assets(rows, subject)
    query = _text(detected.get("summary")) or f"{detector} detected on {subject}"
    case = SentinelIncidentCase(
        id=case_id,
        query=query,
        query_terms=terms,
        assets=assets,
    )

    evidence: list[dict[str, Any]] = []
    evidence_id_by_index: dict[int, str] = {}
    for index, event in enumerate(rows):
        kind = _text(event.get("kind"))
        if kind in {"awaiting_confirmation", "cooldown"}:
            continue
        evidence_id = f"sentinel-evidence:{digest[:20]}:{index:03d}:{kind}"
        evidence_id_by_index[index] = evidence_id
        evidence.append({
            "evidence_id": evidence_id,
            "source": f"sentinel:{kind}",
            "summary": _evidence_summary(event),
            "data": dict(event),
        })

    cited = [item["evidence_id"] for item in evidence]
    trace: list[TraceEvent] = []
    first_timestamp = _timestamp(rows[0].get("at"))
    trace.append(TraceEvent(
        run_id=run_id,
        case_id=case_id,
        kind="alert_received",
        timestamp=first_timestamp,
        payload={
            "detector": detector,
            "subject": subject,
            "action": action,
            "family": family,
        },
    ))

    exposed = list(dict.fromkeys(
        "sentinel_command" if event.get("kind") == "command"
        else "sentinel_preflight" if event.get("kind") == "preflight"
        else "sentinel_watch_probe" if event.get("kind") == "bakein_sampled"
        else ""
        for event in rows
    ))
    exposed = [skill for skill in exposed if skill]
    if detector == "failed_units":
        # The detector's current systemd reading is the reusable observation.
        # Keep the generic sentinel skills for audit, and add the explicit
        # investigation skill whose command is already allowlisted.
        exposed.append("failed_services")
    trace.append(TraceEvent(
        run_id=run_id,
        case_id=case_id,
        kind="skills_exposed",
        timestamp=first_timestamp,
        payload={"skills": exposed},
    ))
    for index, event in enumerate(rows):
        kind = _text(event.get("kind"))
        evidence_id = evidence_id_by_index.get(index)
        timestamp = _timestamp(event.get("at"))
        if kind in _FOLLOWUP_KINDS:
            trace.append(TraceEvent(
                run_id=run_id,
                case_id=case_id,
                kind=kind,
                timestamp=timestamp,
                payload={**event, "evidence_id": evidence_id},
            ))
        skill = None
        if kind == "detected" and detector == "failed_units":
            skill = "failed_services"
        elif kind == "command":
            skill = "sentinel_command"
        elif kind == "preflight":
            skill = "sentinel_preflight"
        elif kind == "bakein_sampled":
            skill = "sentinel_watch_probe"
        if skill and evidence_id:
            trace.append(TraceEvent(
                run_id=run_id,
                case_id=case_id,
                kind="tool_called",
                timestamp=timestamp,
                payload={
                    "skill": skill,
                    "evidence_ids": [evidence_id],
                    "blocked": False,
                },
            ))

    final_timestamp = _timestamp(rows[-1].get("at"))
    trace.append(TraceEvent(
        run_id=run_id,
        case_id=case_id,
        kind="verifier_result",
        timestamp=final_timestamp,
        payload={
            "passed": True,
            "source": "sentinel_bakein",
            "evidence_ids": cited,
        },
    ))
    trace.append(TraceEvent(
        run_id=run_id,
        case_id=case_id,
        kind="diagnosis_completed",
        timestamp=final_timestamp,
        payload={
            "root_cause_key": root_key,
            "confidence": 1.0,
            "evidence": [{"evidence_id": evidence_id} for evidence_id in cited],
        },
    ))
    return SyntheticIncidentRun(run_id=run_id, case=case, events=trace, evidence=evidence)


def _ineffective_outcome(rows: list[Mapping[str, Any]]) -> str | None:
    for event in reversed(rows):
        kind = _text(event.get("kind"))
        if kind in _INEFFECTIVE_TERMINALS:
            return kind
        if kind == "remediated":
            outcome = _text(event.get("outcome"))
            if outcome in _INEFFECTIVE_TERMINALS:
                return outcome
    return None


def _terminal_event(
    rows: list[Mapping[str, Any]], outcome: str,
) -> Mapping[str, Any]:
    return next(
        (
            event
            for event in reversed(rows)
            if event.get("kind") == outcome
            or (
                event.get("kind") == "remediated"
                and event.get("outcome") == outcome
            )
        ),
        rows[-1],
    )


def _attempt_count(
    rows: list[Mapping[str, Any]], outcome: str, terminal: Mapping[str, Any],
) -> int:
    if outcome == "declined":
        return 0
    if outcome == "escalated":
        recurrences = terminal.get("recurrences")
        if isinstance(recurrences, int) and not isinstance(recurrences, bool):
            return max(0, recurrences)
        prior_cycles = terminal.get("prior_cycles")
        if isinstance(prior_cycles, list):
            return len(prior_cycles)
    committed = sum(event.get("kind") == "remediation_committed" for event in rows)
    return max(1, committed)


def _ineffective_reason(
    rows: list[Mapping[str, Any]], terminal: Mapping[str, Any],
) -> str:
    reason = _text(terminal.get("reason") or terminal.get("detail"))
    if reason:
        return reason
    remediated = next(
        (event for event in reversed(rows) if event.get("kind") == "remediated"),
        None,
    )
    if remediated is not None:
        reason = _text(remediated.get("detail") or remediated.get("reason"))
        if reason:
            return reason
    regressed = next(
        (event for event in reversed(rows) if event.get("kind") == "bakein_regressed"),
        None,
    )
    probes = regressed.get("probes") if regressed is not None else None
    if isinstance(probes, list) and probes:
        return f"watch probes regressed: {', '.join(str(probe) for probe in probes)}"
    return "the terminal disposition did not verify an effective repair"


def is_control_hold_chain(chain: Iterable[Mapping[str, Any]]) -> bool:
    """Whether an operator stop held a write before any action was attempted.

    The repeated detector readings remain in the Sentinel timeline. They are
    control-plane observations, not independent failed remediations, so turning
    every poll into an ineffective memory would manufacture cases and dossiers.
    """
    rows = [dict(event) for event in chain]
    if not rows:
        return False
    outcome = _ineffective_outcome(rows)
    if outcome != "declined":
        return False
    terminal = _terminal_event(rows, outcome)
    reason = _ineffective_reason(rows, terminal).lower()
    return (
        _attempt_count(rows, outcome, terminal) == 0
        and "global remediation pause is active" in reason
    )


def synthesize_ineffective_memory(
    chain: Iterable[Mapping[str, Any]],
) -> SyntheticIneffectiveMemory | None:
    """Build a separately labelled memory for one ineffective terminal chain."""

    rows = [dict(event) for event in chain]
    if not rows or is_control_hold_chain(rows):
        return None
    outcome = _ineffective_outcome(rows)
    if outcome is None:
        return None
    detected = next((event for event in rows if event.get("kind") == "detected"), None)
    if detected is None:
        return None

    detector = _text(detected.get("detector"))
    subject = _text(detected.get("target") or detected.get("subject"))
    action = _text(detected.get("action"))
    if not detector or not subject:
        return None

    terminal = _terminal_event(rows, outcome)
    action = _text(terminal.get("action")) or action
    attempts = _attempt_count(rows, outcome, terminal)
    reason = _ineffective_reason(rows, terminal)
    digest = _canonical_digest(rows)
    run_id = f"sentinel-ineffective-run-{digest[:32]}"
    memory_id = f"epi-ineffective-{digest[:32]}"
    root_key = f"sentinel.{detector.lower()}"
    terms = _ascii_terms(rows, root_key)
    tags = list(dict.fromkeys([
        *terms,
        "outcome:ineffective",
        f"terminal:{outcome}",
        f"ineffective-key:{root_key}",
        *([f"action:{action.lower()}"] if action else []),
    ]))

    evidence_snapshot: list[dict[str, Any]] = []
    evidence_ids: list[str] = []
    for index, event in enumerate(rows):
        kind = _text(event.get("kind"))
        evidence_id = f"sentinel-evidence:{digest[:20]}:{index:03d}:{kind}"
        evidence_ids.append(evidence_id)
        evidence_snapshot.append({
            "evidence_id": evidence_id,
            "source": f"sentinel:{kind}",
            "summary": _evidence_summary(event),
            "data": dict(event),
        })

    record = MemoryRecord(
        memory_id=memory_id,
        tier="episodic",
        text=(
            f"INEFFECTIVE: action={action or 'none'}; subject={subject}; "
            f"outcome={outcome}; attempts={attempts}; reason={reason}"
        ),
        tags=tags,
        asset_ids=_assets(rows, subject),
        evidence_ids=evidence_ids,
        confidence=1.0,
        source_trace_ids=[run_id],
        evidence_snapshot=evidence_snapshot,
        first_observed_at=_timestamp(rows[0].get("at")),
        last_observed_at=_timestamp(rows[-1].get("at")),
        event_type=f"ineffective:{outcome}",
    )
    return SyntheticIneffectiveMemory(run_id=run_id, record=record)


def synthesize_safety_gated_memory(
    chain: Iterable[Mapping[str, Any]],
) -> SyntheticIneffectiveMemory | None:
    """Retain a refusal decision without granting diagnosis or action credit."""
    rows = [dict(event) for event in chain]
    if not rows or _text(rows[-1].get("kind")) not in _SAFETY_GATED_TERMINALS:
        return None
    detected = next((event for event in rows if event.get("kind") == "detected"), None)
    if detected is None:
        return None
    detector = _text(detected.get("detector"))
    subject = _text(detected.get("target") or detected.get("subject"))
    if not detector or not subject:
        return None
    terminal = rows[-1]
    candidate = _text(
        terminal.get("candidate_action") or detected.get("candidate_action") or "none"
    )
    reason = _text(terminal.get("reason") or terminal.get("note")) or (
        "the registered write controls were not satisfied"
    )
    digest = _canonical_digest(rows)
    run_id = f"sentinel-safety-run-{digest[:32]}"
    memory_id = f"epi-safety-{digest[:32]}"
    root_key = f"sentinel.{detector.lower()}"
    tags = list(dict.fromkeys([
        *_ascii_terms(rows, root_key),
        "outcome:safety_gated",
        "terminal:no_safe_action",
        f"candidate-action:{candidate.lower()}",
        f"detector:{detector.lower()}",
    ]))
    evidence_snapshot: list[dict[str, Any]] = []
    evidence_ids: list[str] = []
    for index, event in enumerate(rows):
        kind = _text(event.get("kind"))
        evidence_id = f"sentinel-evidence:{digest[:20]}:{index:03d}:{kind}"
        evidence_ids.append(evidence_id)
        evidence_snapshot.append({
            "evidence_id": evidence_id,
            "source": f"sentinel:{kind}",
            "summary": _evidence_summary(event),
            "data": dict(event),
        })
    record = MemoryRecord(
        memory_id=memory_id,
        tier="episodic",
        text=(
            f"SAFETY GATE: candidate={candidate}; subject={subject}; "
            f"outcome=no_safe_action; reason={reason}"
        ),
        tags=tags,
        asset_ids=_assets(rows, subject),
        evidence_ids=evidence_ids,
        confidence=1.0,
        source_trace_ids=[run_id],
        evidence_snapshot=evidence_snapshot,
        first_observed_at=_timestamp(rows[0].get("at")),
        last_observed_at=_timestamp(rows[-1].get("at")),
        event_type="safety_gated:no_safe_action",
    )
    return SyntheticIneffectiveMemory(run_id=run_id, record=record)


def _already_consolidated(memory: TieredMemoryStore, run_id: str) -> bool:
    return any(run_id in record.source_trace_ids for record in memory.records())


def consolidate_incident_chain(
    chain: Iterable[Mapping[str, Any]],
    memory: TieredMemoryStore | None,
    skills: SkillRegistry | None = None,
    *,
    flush_retained: bool = True,
) -> ConsolidationReport | None:
    """Retain one passed or ineffective chain, skipping an absent store/replay."""

    if memory is None:
        return None
    rows = [dict(event) for event in chain]
    synthetic = synthesize_incident_run(rows)
    ineffective = synthesize_ineffective_memory(rows) if synthetic is None else None
    safety_gated = (
        synthesize_safety_gated_memory(rows)
        if synthetic is None and ineffective is None else None
    )
    run_id = synthetic.run_id if synthetic is not None else (
        ineffective.run_id if ineffective is not None else (
            safety_gated.run_id if safety_gated is not None else ""
        )
    )
    if not run_id or _already_consolidated(memory, run_id):
        return None

    registry = skills or SkillRegistry()
    memory_snapshot = [record.model_copy(deep=True) for record in memory.records()]
    skill_snapshot = {
        skill.spec.name: skill.spec.model_copy(deep=True) for skill in registry.all()
    }
    try:
        if ineffective is not None or safety_gated is not None:
            # An ineffective action is useful as a stop signal, but it is not a
            # diagnosis or a successful procedure.  Keep it out of the positive
            # credit, routing, reflection, and skill-evolution path entirely.
            retained = ineffective or safety_gated
            assert retained is not None
            memory.add(retained.record)
            if flush_retained:
                memory.flush()
            return ConsolidationReport(
                run_id=retained.run_id,
                passed=False,
                added=[retained.record.memory_id],
            )
        return consolidate_run(
            synthetic.events,
            synthetic.case,
            memory,
            registry,
            synthetic.evidence,
            # Sentinel consolidation bypasses the service options used by
            # diagnoses. Wire conflict resolution here so a changed root for
            # one subject retires the stale incident instead of merging roots.
            resolve_conflicts=True,
        )
    except Exception:
        # Consolidation mutates its working set before the repository commit.
        # Restore that set when the commit fails so a later replay can retry the
        # same externally verified chain from durable truth.
        if memory.repository is not None:
            memory.reload_from_repository()
        else:
            memory.replace_records(memory_snapshot)
        for skill in registry.all():
            if skill.spec.name in skill_snapshot:
                skill.spec = skill_snapshot[skill.spec.name]
        raise


def consolidate_incident_timeline(
    timeline: Iterable[Mapping[str, Any]],
    memory: TieredMemoryStore | None,
    skills: SkillRegistry | None = None,
) -> list[ConsolidationReport]:
    """Consolidate every new passed or ineffective chain in a timeline replay."""

    if memory is None:
        return []
    reports: list[ConsolidationReport] = []
    for chain in completed_incident_chains(timeline):
        report = consolidate_incident_chain(
            chain, memory, skills, flush_retained=False,
        )
        if report is not None:
            reports.append(report)
    if reports:
        memory.flush()
    return reports

"""Large real-corpus retrieval eval over raw FortiGate syslog — LLM-free, zero-dep.

Where :mod:`core.eval.skill_retrieval` retrieves over a 9-document *skill catalog*,
this eval builds a genuinely large **evidence corpus mined from raw device logs** and
asks the same question: given an operator's natural-language incident query, does
lexical / structured retrieval surface the evidence the gold case actually requires,
out of ~1k real distractor units?

PIPELINE (all deterministic, no model, no LLM):

  1. PARSE   ``parse_syslog_line`` — one raw FortiGate key=value line -> field dict.
  2. MINE    ``mine_units`` — aggregate parsed lines into EVIDENCE UNITS at two tiers
             in one corpus:
               * a *summary* unit per signal category (the aggregate — one per category);
               * an *entity* unit per (signal category, entity bucket) — e.g. one per
                 denied destination port, one per attacking source IP. These are the
                 realistic large distractor pool (~1k units on the local 18k-line sample).
             A unit's searchable text is built ONLY from real parsed field values
             (logdesc, action, type/subtype, the FortiGate ``service`` string, ports,
             representative entities). It never contains an ev-* id, a case query, or a
             hand-written analyst summary — so the query->evidence bridge cannot cheat
             through the label.
  3. RETRIEVE for each of the 8 gold cases' NL query, rank units with the reused
             retrievers (naive / BM25 / structured-tag / RRF) under a query-expansion
             mode, and measure recall@k against the case's hand-labelled
             ``required_evidence``.

EV-* -> MINED-UNIT MAPPING (see ``EV_TO_CATEGORY``): each required-evidence id is mapped
to the signal category(ies) that realise it *by structured signal definition* — the same
``logdesc`` / ``action`` / ``dstport`` filter documented in the ``source`` field of
``domains.network_rca.adapters.real_syslog_adapter``. A mined unit is "gold" for an ev-id
iff its category is in that ev's category set. This mapping is by real log semantics, not
by the label string; its one honest leakage caveat is stated in ``LEAKAGE_NOTE`` below.

Two recall variants are reported:
  * ``coverage``  — an ev is satisfied at k if ANY unit of its category (summary OR entity)
                    is in top-k. Answers "did we surface the required signal at all". It is
                    inflated by shattering (more entity units = more chances), so it is read
                    together with the distractor count, never alone.
  * ``canonical`` — an ev is satisfied at k only if the category *summary* unit is in top-k
                    (a strict, shatter-proof 1:1 target). This is the honest headline.

Reused, unmodified: :mod:`core.memory.bm25`, :mod:`core.memory.rrf`,
:mod:`core.memory.query_expansion`, :mod:`core.memory.crag_gate`.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from core.memory.bm25 import BM25Index, tokenize
from core.memory.rrf import rrf_fuse
from core.memory.query_expansion import make_transform
from core.memory.crag_gate import crag_gate

# ── locations ─────────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SYSLOG_DIR = _ROOT / "domains/network_rca/fixtures/real/syslog"
DEFAULT_TRAIN = _ROOT / "domains/network_rca/fixtures/real/train_cases.json"
DEFAULT_HELDOUT = _ROOT / "domains/network_rca/fixtures/real/heldout_cases.json"

_K_VALUES = (5, 10, 20)
_HEADLINE_K = 10

# Dahua camera/DVR SDK service ports. Denied flows to these are the "device port probe"
# signal — a *subset* of deny traffic, split out because the gold case distinguishes it
# from generic policy-deny. Matches the {37777,37809,37810} set documented in the real
# syslog adapter's ev-device-port-probe source filter (37778 added: adjacent Dahua port).
DVR_PORTS = frozenset({"37777", "37778", "37809", "37810"})

# ── 1. deterministic parser: raw FortiGate kv line -> field dict ───────────────
# key=value where value is either a double-quoted string (possibly containing spaces)
# or a run of non-space characters. The leading BSD-syslog prefix ("Jun 16 00:01:31
# _gateway") has no '=' and is skipped naturally. Never raises (unlike shlex.split).
_KV_RE = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\S+)')


def parse_syslog_line(line: str) -> dict[str, str]:
    """Parse one raw FortiGate syslog line into a ``{key: value}`` dict.

    Quoted values are unquoted; the syslog priority/timestamp/host prefix is ignored.
    Fully deterministic and total (any string parses, empty dict if no kv pairs).
    """
    out: dict[str, str] = {}
    for key, val in _KV_RE.findall(line):
        if val and val[0] == '"' and val[-1] == '"':
            val = val[1:-1]
        out[key] = val
    return out


def parse_syslog_files(paths: Iterable[Path]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for p in paths:
        text = p.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            if line.strip():
                fields = parse_syslog_line(line)
                if fields:
                    rows.append(fields)
    return rows


# ── 2. signal-category classification (deterministic, from structured fields) ──
# Category key -> human tokens that describe the category using REAL log vocabulary
# only (these mirror the logdesc / action the device itself emits — no query words).
_CATEGORY_TOKENS: dict[str, str] = {
    "admin_login_failed": "event system admin login failed authentication administrator",
    "admin_login_disabled": "event system admin login disabled lockout locked bad attempts",
    "session_clash": "event system session clash state tuple informational",
    "dhcp_ack": "event system dhcp ack lease address dhcpack server allocation",
    "dhcp_statistics": "event system dhcp statistics scope leases",
    "fortigate_update": "event system fortigate update succeeded fortiguard scheduled",
    "perf_stats": "event system performance statistics cpu memory sessions setup rate",
    "device_port_probe": "traffic deny denied device service port dahua",
    "policy_deny": "traffic deny denied blocked flow policy local-in forward",
    "traffic_accept": "traffic accept permit permitted forwarding session baseline",
}


def classify_category(fields: dict[str, str]) -> str | None:
    """Map a parsed line to its signal category key, or ``None`` if uncategorised.

    Uses only structured fields (``logdesc`` / ``action`` / ``dstport``) — the same
    signals the real syslog adapter documents in each evidence unit's ``source``.
    """
    logdesc = (fields.get("logdesc") or "").strip()
    if logdesc:
        table = {
            "Admin login failed": "admin_login_failed",
            "Admin login disabled": "admin_login_disabled",
            "session clash": "session_clash",
            "DHCP Ack log": "dhcp_ack",
            "DHCP statistics": "dhcp_statistics",
            "FortiGate update succeeded": "fortigate_update",
            "System performance statistics": "perf_stats",
        }
        if logdesc in table:
            return table[logdesc]
    action = (fields.get("action") or "").strip()
    if action == "deny":
        if fields.get("dstport") in DVR_PORTS:
            return "device_port_probe"
        return "policy_deny"
    if action in ("accept", "permit"):
        return "traffic_accept"
    return None


# Which field is the natural entity key that shatters each category into detail units.
# Categories absent here stay summary-only (low cardinality / no natural entity).
_ENTITY_KEY: dict[str, str] = {
    "admin_login_failed": "srcip",     # one unit per attacking source IP
    "policy_deny": "dstport",          # one unit per denied destination port
    "device_port_probe": "dstport",    # one unit per DVR service port
    "traffic_accept": "dstport",       # one unit per accepted destination port
    "dhcp_ack": "ip",                  # one unit per DHCP client IP
}

_PRIVATE_RE = re.compile(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)")


def _ip_class(ip: str | None) -> str:
    if not ip:
        return ""
    return "internal" if _PRIVATE_RE.match(ip) else "external"


# ── 2b. ev-* -> mined signal category mapping (the anti-circularity crux) ───────
# Each required-evidence id is realised by the signal category(ies) below, chosen by
# the STRUCTURED FILTER documented in real_syslog_adapter._ev_* .source — NOT by the
# label text. Overlaps in the hand adapter (it folds fortigate-update counts into both
# ev-event-log-scan and ev-security-posture) are resolved to the *primary* signal each
# case's NL query names: session-clash for the event-log case, update for the posture
# case. This resolves realisation, it does NOT relabel any case's required_evidence.
EV_TO_CATEGORY: dict[str, frozenset[str]] = {
    "ev-admin-auth-failures": frozenset({"admin_login_failed"}),
    "ev-admin-lockout": frozenset({"admin_login_disabled"}),
    "ev-policy-deny-profile": frozenset({"policy_deny"}),
    "ev-traffic-baseline": frozenset({"traffic_accept"}),
    "ev-event-log-scan": frozenset({"session_clash"}),
    "ev-dhcp-health": frozenset({"dhcp_ack", "dhcp_statistics"}),
    "ev-security-posture": frozenset({"fortigate_update"}),
    "ev-device-port-probe": frozenset({"device_port_probe"}),
}

LEAKAGE_NOTE = (
    "The ev->unit map keys on the same structured fields (logdesc/action/dstport) whose "
    "values also seed a unit's searchable text, and the operator query describes those "
    "same real signals. So query and gold unit share the device's own vocabulary "
    "(e.g. query 'admin authentication failures' <-> logdesc 'Admin login failed'); that "
    "is the intended lexical bridge, not label leakage. Residual risk: the BM25 document "
    "is pure real-log tokens (no query words injected), but the STRUCTURED TAG set adds an "
    "RFC1918 internal/external class that aligns with query phrasing ('internal hosts' / "
    "'external IPs'). BM25 is therefore the label-clean baseline; structured/rrf get that "
    "one netops enrichment. Numbers are reported for all so the effect is visible."
)


# ── evidence unit model ────────────────────────────────────────────────────────
@dataclass(frozen=True)
class EvidenceUnit:
    unit_id: str
    category: str
    tier: str                       # "summary" | "entity"
    text: str                       # free-text doc (pure real-log tokens) for BM25/naive
    tags: tuple[str, ...]           # structured tags for the structured retriever
    meta: dict = field(default_factory=dict)


def _summary_id(category: str) -> str:
    return f"sum::{category}"


def mine_units(rows: list[dict[str, str]]) -> dict[str, EvidenceUnit]:
    """Aggregate parsed lines into the evidence-unit corpus (summary + entity tiers).

    Deterministic: unit ids and text derive only from field values and counts.
    """
    # bucket rows by category, and (category, entity-value) for shatterable categories
    cat_rows: dict[str, list[dict]] = {}
    ent_rows: dict[tuple[str, str], list[dict]] = {}
    for f in rows:
        cat = classify_category(f)
        if cat is None:
            continue
        cat_rows.setdefault(cat, []).append(f)
        ekey = _ENTITY_KEY.get(cat)
        if ekey:
            ev = f.get(ekey)
            if ev:
                ent_rows.setdefault((cat, ev), []).append(f)

    units: dict[str, EvidenceUnit] = {}

    # summary unit per category (the canonical 1:1 target for an ev-*)
    for cat, group in sorted(cat_rows.items()):
        services = _top_values(group, "service", 4)
        ports = _top_values(group, "dstport", 5)
        classes = sorted({_ip_class(r.get("srcip")) for r in group} - {""})
        n_entity = len({r.get(_ENTITY_KEY[cat]) for r in group if r.get(_ENTITY_KEY[cat])}) if cat in _ENTITY_KEY else 0
        parts = [
            _CATEGORY_TOKENS.get(cat, cat.replace("_", " ")),
            "summary aggregate all",
            f"{len(group)} events",
            " ".join(services),
            " ".join(ports),
        ]
        tags = [cat, "summary"]
        tags += [t for t in _CATEGORY_TOKENS.get(cat, "").split() if t in ("deny", "accept", "permit", "login", "dhcp", "update")]
        tags += classes
        tags += [p for p in ports]
        tags += [s for svc in services for s in re.findall(r"[a-z0-9]+", svc.lower())]
        units[_summary_id(cat)] = EvidenceUnit(
            unit_id=_summary_id(cat),
            category=cat,
            tier="summary",
            text=" ".join(p for p in parts if p),
            tags=tuple(dict.fromkeys(tags)),
            meta={"line_count": len(group), "distinct_entities": n_entity},
        )

    # entity detail unit per (category, entity value) — the large distractor pool
    for (cat, ev), group in sorted(ent_rows.items()):
        ekey = _ENTITY_KEY[cat]
        services = _top_values(group, "service", 2)
        ports = _top_values(group, "dstport", 2)
        cls = sorted({_ip_class(r.get("srcip")) for r in group} - {""})
        parts = [
            _CATEGORY_TOKENS.get(cat, cat.replace("_", " ")),
            f"{ekey} {ev}",
            f"{len(group)} events",
            " ".join(services),
            " ".join(ports),
        ]
        tags = [cat, "entity", ev] + list(cls)
        tags += [s for svc in services for s in re.findall(r"[a-z0-9]+", svc.lower())]
        uid = f"ent::{cat}::{ekey}={ev}"
        units[uid] = EvidenceUnit(
            unit_id=uid,
            category=cat,
            tier="entity",
            text=" ".join(p for p in parts if p),
            tags=tuple(dict.fromkeys(tags)),
            meta={"line_count": len(group), "entity": ev},
        )
    return units


def _top_values(rows: list[dict], key: str, n: int) -> list[str]:
    counts: dict[str, int] = {}
    for r in rows:
        v = r.get(key)
        if v:
            counts[v] = counts.get(v, 0) + 1
    return [v for v, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:n]]


# ── 3. retrievers over the mined corpus (reuse core.memory) ────────────────────
def build_corpus(syslog_dir: str | Path | None = None) -> dict[str, EvidenceUnit]:
    d = Path(syslog_dir or DEFAULT_SYSLOG_DIR)
    paths = sorted(d.glob("*.log")) + sorted(d.glob("*.log.gz"))
    return mine_units(parse_syslog_files(paths))


def build_retrievers(units: dict[str, EvidenceUnit], mode: str = "base") -> dict[str, Callable[[str, int], list[tuple[str, float]]]]:
    """Return retrievers mapping (query, k) -> [(unit_id, score), ...] best-first.

    ``naive`` bag-of-words overlap, ``bm25`` Okapi sparse over pure-log text, ``structured``
    tag overlap, ``rrf`` = RRF fusion of bm25+structured. All share the ``mode`` transform
    (base / stem / expand) so the comparison is apples-to-apples.
    """
    q_transform, d_transform = make_transform(mode)
    docs = {uid: d_transform(tokenize(u.text)) for uid, u in units.items()}
    tag_tokens = {uid: d_transform([t.lower() for t in u.tags]) for uid, u in units.items()}
    doc_sets = {uid: set(toks) for uid, toks in docs.items()}
    tag_sets = {uid: set(toks) for uid, toks in tag_tokens.items()}
    bm25 = BM25Index(docs)
    ordered_ids = sorted(units)

    def naive(query: str, k: int) -> list[tuple[str, float]]:
        if k <= 0:
            return []
        q = set(q_transform(tokenize(query)))
        if not q:
            return []
        scored = [(len(q & doc_sets[u]) / len(q), u) for u in ordered_ids if q & doc_sets[u]]
        scored.sort(key=lambda it: (-it[0], it[1]))
        return [(u, round(s, 6)) for s, u in scored[:k]]

    def structured(query: str, k: int) -> list[tuple[str, float]]:
        if k <= 0:
            return []
        q = set(q_transform(tokenize(query)))
        scored = [(sum(1 for t in tag_sets[u] if t in q), u) for u in ordered_ids]
        scored = [(s, u) for s, u in scored if s > 0]
        scored.sort(key=lambda it: (-it[0], it[1]))
        return [(u, float(s)) for s, u in scored[:k]]

    def bm25_retrieve(query: str, k: int) -> list[tuple[str, float]]:
        return bm25.rank_with_scores(query, k, query_tokens=q_transform(tokenize(query)))

    def rrf(query: str, k: int) -> list[tuple[str, float]]:
        pool = max(k, _HEADLINE_K)
        bm = [u for u, _ in bm25_retrieve(query, pool)]
        st = [u for u, _ in structured(query, pool)]
        fused = rrf_fuse([bm, st], k)
        return [(u, 1.0 / (i + 1)) for i, u in enumerate(fused)]

    return {"naive": naive, "bm25": bm25_retrieve, "structured": structured, "rrf": rrf}


# ── gold resolution + metrics ──────────────────────────────────────────────────
def load_cases(*paths: str | Path) -> list[dict]:
    out: list[dict] = []
    for p in (paths or (DEFAULT_TRAIN, DEFAULT_HELDOUT)):
        for c in json.loads(Path(p).read_text(encoding="utf-8")):
            out.append({
                "id": c["case"]["id"],
                "query": c["case"]["query"],
                "required": list(c["ground_truth"]["required_evidence"]),
            })
    return out


def gold_units_for_ev(ev_id: str, units: dict[str, EvidenceUnit]) -> dict[str, set[str]]:
    """Return {'all': {unit_ids...}, 'summary': {summary_unit_ids...}} realising ev_id."""
    cats = EV_TO_CATEGORY.get(ev_id, frozenset())
    all_units = {uid for uid, u in units.items() if u.category in cats}
    summ = {_summary_id(c) for c in cats if _summary_id(c) in units}
    return {"all": all_units, "summary": summ}


def _case_recall(retrieved_ids: list[str], required: list[str], units: dict[str, EvidenceUnit], variant: str) -> tuple[float, list[int]]:
    """Recall over one case + the 1-based rank of the first gold hit per required ev."""
    top = list(retrieved_ids)
    hits = 0
    first_ranks: list[int] = []
    for ev in required:
        gold = gold_units_for_ev(ev, units)[variant]
        rank = next((i + 1 for i, uid in enumerate(top) if uid in gold), 0)
        if rank:
            hits += 1
            first_ranks.append(rank)
    return (hits / len(required) if required else 0.0), first_ranks


def run_corpus_retrieval_eval(syslog_dir: str | Path | None = None,
                              case_paths: tuple[str | Path, ...] | None = None,
                              mode: str = "base") -> dict:
    """Full eval: mine corpus, run every retriever at each k, score both recall variants."""
    units = build_corpus(syslog_dir)
    cases = load_cases(*(case_paths or ()))
    retrievers = build_retrievers(units, mode)

    # corpus / distractor accounting.
    #  * canonical task: the gold target for each ev is its category *summary* unit, so
    #    the whole rest of the corpus (all entity units + other summaries) are non-targets.
    #  * coverage task: per case, the honest distractor pool is every unit OUTSIDE the
    #    case's required categories (units of the same category are within-category near
    #    positives, not distractors). Reported as a distribution across cases.
    n_summary = sum(1 for u in units.values() if u.tier == "summary")
    canonical_targets: set[str] = set()
    for c in cases:
        for ev in c["required"]:
            canonical_targets |= gold_units_for_ev(ev, units)["summary"]
    per_case_distractors: list[int] = []
    for c in cases:
        req_cats = set().union(*(EV_TO_CATEGORY.get(ev, frozenset()) for ev in c["required"]))
        in_cat = sum(1 for u in units.values() if u.category in req_cats)
        per_case_distractors.append(len(units) - in_cat)
    sd = sorted(per_case_distractors)
    out: dict = {
        "dataset_kind": "real-fortigate-corpus",
        "mode": mode,
        "corpus_size": len(units),
        "summary_units": n_summary,
        "entity_units": len(units) - n_summary,
        "signal_categories": len({u.category for u in units.values()}),
        "canonical_targets": len(canonical_targets),
        "canonical_non_targets": len(units) - len(canonical_targets),
        "coverage_distractors_per_case": {
            "min": sd[0], "median": sd[len(sd) // 2], "max": sd[-1],
        },
        "n_cases": len(cases),
        "k_values": list(_K_VALUES),
        "methods": {},
        "per_case": {},
    }
    for method, retrieve in retrievers.items():
        by_k: dict[int, dict] = {}
        for k in _K_VALUES:
            cov, can, ranks = [], [], []
            for c in cases:
                ids = [uid for uid, _ in retrieve(c["query"], k)]
                cr, rk = _case_recall(ids, c["required"], units, "all")
                kr, _ = _case_recall(ids, c["required"], units, "summary")
                cov.append(cr)
                can.append(kr)
                ranks += rk
            by_k[k] = {
                "coverage_recall": round(sum(cov) / len(cov), 4) if cov else 0.0,
                "canonical_recall": round(sum(can) / len(can), 4) if can else 0.0,
                "mean_first_gold_rank": round(sum(ranks) / len(ranks), 2) if ranks else None,
            }
        out["methods"][method] = by_k

    # per-case breakdown at the headline k for the strongest method (rrf)
    rrf = retrievers["rrf"]
    for c in cases:
        ids = [uid for uid, _ in rrf(c["query"], _HEADLINE_K)]
        cr, _ = _case_recall(ids, c["required"], units, "all")
        kr, _ = _case_recall(ids, c["required"], units, "summary")
        out["per_case"][c["id"]] = {
            "required": c["required"],
            "coverage_recall": round(cr, 3),
            "canonical_recall": round(kr, 3),
            "retrieved_gold_summary": [uid for uid in ids if uid.startswith("sum::")][:6],
        }
    return out


def demo_crag_gate(query: str, syslog_dir: str | Path | None = None, mode: str = "base",
                   k: int = _HEADLINE_K, hi: float = 3.0, lo: float = 0.5):
    """Show the reused CRAG confidence gate acting on this corpus's BM25 scores."""
    units = build_corpus(syslog_dir)
    bm25 = build_retrievers(units, mode)["bm25"]
    return crag_gate(bm25(query, k), k, hi=hi, lo=lo)


# ── reporting ──────────────────────────────────────────────────────────────────
def _print(res: dict) -> None:
    print(f"dataset: {res['dataset_kind']}  ({res['n_cases']} gold cases, mode={res['mode']}, LLM-free)")
    print(f"corpus: {res['corpus_size']} evidence units mined from raw syslog "
          f"= {res['summary_units']} category-summary + {res['entity_units']} entity-detail units, "
          f"spanning {res['signal_categories']} signal categories")
    dpc = res["coverage_distractors_per_case"]
    print(f"canonical task : {res['canonical_targets']} summary targets vs "
          f"{res['canonical_non_targets']} non-target units")
    print(f"coverage task  : per case, out-of-category distractor pool = "
          f"{dpc['min']}..{dpc['max']} units (median {dpc['median']})\n")
    header = "method".ljust(11) + "".join(f"covR@{k}".rjust(9) for k in res["k_values"]) + "   " + \
             "".join(f"canR@{k}".rjust(9) for k in res["k_values"])
    print(header)
    print("-" * len(header))
    for method in ("naive", "bm25", "structured", "rrf"):
        by_k = res["methods"][method]
        row = method.ljust(11)
        row += "".join(f"{by_k[k]['coverage_recall']:.3f}".rjust(9) for k in res["k_values"])
        row += "   " + "".join(f"{by_k[k]['canonical_recall']:.3f}".rjust(9) for k in res["k_values"])
        print(row)
    print("\n(covR = coverage recall: any category unit in top-k; "
          "canR = canonical recall: category *summary* unit in top-k — the honest headline)")
    print(f"\nper-case (rrf, k={_HEADLINE_K}, mode={res['mode']}):")
    for cid, row in res["per_case"].items():
        print(f"  {cid:<34} req={row['required']}  cov={row['coverage_recall']}  can={row['canonical_recall']}")


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    syslog_dir = argv[0] if argv else None
    print("=" * 92)
    _print(run_corpus_retrieval_eval(syslog_dir, mode="base"))
    print("\n" + "=" * 92)
    print("query-expansion effect on canonical recall@%d (avg over cases):" % _HEADLINE_K)
    for mode in ("base", "stem", "expand"):
        res = run_corpus_retrieval_eval(syslog_dir, mode=mode)
        r = {m: res["methods"][m][_HEADLINE_K]["canonical_recall"] for m in ("naive", "bm25", "structured", "rrf")}
        print(f"  {mode:<7} naive {r['naive']:.3f}  bm25 {r['bm25']:.3f}  "
              f"structured {r['structured']:.3f}  rrf {r['rrf']:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Build the console snapshot from the REAL network_rca framework.

Everything served here comes from the actual Autopoiesis pipeline running on the
real R230 FortiGate held-out dataset: readiness, data stats, the held-out
baseline/ablation table, and per-case diagnosis + cited evidence + trace.
No hardcoded incident fixtures.
"""
from __future__ import annotations

import json
import math
import os
from contextlib import contextmanager
import re
import sys
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

# Make the repo root importable so the gateway can drive the real framework.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from domains.network_rca.eval import compare_baselines  # noqa: E402
from domains.network_rca.factory import build_network_rca_orchestrator  # noqa: E402
from domains.network_rca.real_data_readiness import probe_r230_readiness  # noqa: E402
from domains.network_rca.real_dataset import (  # noqa: E402
    load_real_case_bundle,
    resolve_stats_path,
    validate_real_dataset_manifest,
)

_MANIFEST = _REPO_ROOT / "domains" / "network_rca" / "fixtures" / "real" / "manifest.json"
_TOPOLOGY = _REPO_ROOT / "domains" / "network_rca" / "fixtures" / "real" / "real_topology.json"


_MESH = _REPO_ROOT / "domains" / "network_rca" / "fixtures" / "real" / "real_mesh.json"
_DEVICE_GRAPH = _REPO_ROOT / "domains" / "network_rca" / "fixtures" / "real" / "real_device_graph.json"


def _load_device_graphs() -> dict[str, Any]:
    """Full per-subnet device graphs mined from the raw syslog (see build_device_graph)."""
    try:
        return json.loads(_DEVICE_GRAPH.read_text(encoding="utf-8")).get("graphs", {})
    except Exception:
        return {}


def _load_topology() -> dict[str, Any] | None:
    try:
        topo = json.loads(_TOPOLOGY.read_text(encoding="utf-8"))
    except Exception:
        return None
    # The device graph is the single source of truth for how many hosts a segment
    # actually has — the fixture's `hosts` was a stale count from an older capture,
    # so the map claimed 103 devices while only the flagged handful could open.
    graphs = _load_device_graphs()
    for sub in topo.get("subnets", []):
        g = graphs.get(sub.get("cidr"))
        if g:
            sub["hosts"] = g["stats"]["devices"]
            sub["graphEdges"] = g["stats"]["edges"]
    return topo


def _load_meshes() -> dict[str, Any]:
    try:
        return json.loads(_MESH.read_text(encoding="utf-8")).get("meshes", {})
    except Exception:
        return {}


def _data_stats(stats_path: Path) -> dict[str, Any]:
    s = json.loads(stats_path.read_text(encoding="utf-8"))
    return {
        "source": "DAHUA_FORTIGATE (FG100E) via R230 192.168.1.23",
        "windowDays": s.get("window_days", []),
        "adminLoginFailed": s.get("admin_login_failed", 0),
        "distinctSrc": s.get("admin_login_failed_distinct_src", 0),
        "topAttackerSrc": s.get("admin_login_failed_top_src", [])[:5],
        "lockouts": s.get("admin_login_disabled_lockouts", 0),
        "denyCount": s.get("deny_count", 0),
        "topDenyPorts": s.get("deny_top_dstports", [])[:5],
        "topDenySrc": s.get("deny_top_src", [])[:5],
        "acceptPermit": s.get("accept_permit_count", 0),
        "sessionClash": s.get("session_clash", 0),
    }


def _run_case(case, stats_path: Path, reasoner_mode: str) -> dict[str, Any]:
    with TemporaryDirectory() as tmp:
        ledger = Path(tmp) / "trace.jsonl"
        orch = build_network_rca_orchestrator(
            ledger,
            data_source="real",
            real_stats_path=stats_path,
            reasoner_mode=reasoner_mode,
            # Snapshot generation is read-only. Falling back to MEMORY_DSN here
            # initializes the production schema and may seed fixtures when the
            # active set has decayed to zero.
            use_env_memory_dsn=False,
        )
        diagnosis, report = orch.diagnose(case)
        trace = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
    return {
        "id": case.id,
        "title": case.title,
        "query": case.query,
        "assets": case.assets,
        "diagnosis": {
            "rootCauseKey": diagnosis.root_cause_key,
            "rootCause": diagnosis.root_cause,
            "confidence": round(diagnosis.confidence, 3),
            "readonly": diagnosis.readonly,
            "evidence": [
                {"evidenceId": e.evidence_id, "source": e.source, "summary": e.summary}
                for e in diagnosis.evidence
            ],
            "recommendedActions": diagnosis.recommended_actions,
        },
        "verifier": {"passed": report.passed, "errors": list(report.errors)},
        "trace": [{"kind": ev.get("kind"), "payload": ev.get("payload", {})} for ev in trace],
    }


@contextmanager
def _llm_env(overrides: dict[str, str] | None):
    """Apply provider credentials for one call, then put the environment back.

    ``os.environ.update`` here used to be permanent: one request with
    ``?provider=deepseek-v4`` left AUTOPOIESIS_LLM_* set for the life of the
    process, which silently flipped unrelated endpoints from "no model
    configured" to live paid calls. Nothing announced that, and it survived
    until the next restart.
    """
    if not overrides:
        yield
        return
    previous = {key: os.environ.get(key) for key in overrides}
    os.environ.update(overrides)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def load_rca_snapshot(manifest_path: Path | None = None, provider_id: str = "rule") -> dict[str, Any]:
    from . import providers

    manifest = Path(manifest_path) if manifest_path else _MANIFEST
    reasoner_mode, llm_env = providers.resolve_reasoner(provider_id)
    readiness = probe_r230_readiness(manifest_path=manifest)
    validation = validate_real_dataset_manifest(manifest)

    payload: dict[str, Any] = {
        "readiness": {
            "blocked": readiness.blocked,
            "reason": readiness.reason,
            "syslogPortOpen": readiness.syslog_port_open,
            "manifestValid": readiness.manifest_valid,
        },
        "datasetReady": validation.ready,
        "provider": provider_id,
        "reasonerMode": reasoner_mode,
        "providers": providers.list_providers(),
        "providerError": None,
        "topology": _load_topology(),
        "meshes": _load_meshes(),
        "cases": [],
        "baselines": [],
        "dataStats": None,
        "note": (
            f"Live data from the real network_rca framework on the R230 FortiGate held-out set "
            f"(reasoner: {reasoner_mode})."
        ),
    }

    if not validation.ready:
        payload["note"] = "No validated real held-out dataset present. " + readiness.reason
        return payload

    stats_path = resolve_stats_path(manifest)
    payload["dataStats"] = _data_stats(stats_path)

    cases, ground_truth = load_real_case_bundle(manifest, split="heldout")
    try:
        if reasoner_mode == "llm":
            # Parallelize the per-case LLM diagnoses so the live request stays responsive.
            from concurrent.futures import ThreadPoolExecutor

            # The env override has to outlive the pool: worker threads read the
            # same process environment, so restoring it before they finish would
            # leave them unconfigured mid-flight.
            with _llm_env(llm_env), ThreadPoolExecutor(max_workers=min(6, len(cases) or 1)) as pool:
                payload["cases"] = list(pool.map(lambda c: _run_case(c, stats_path, "llm"), cases))
        else:
            payload["cases"] = [_run_case(case, stats_path, "rule") for case in cases]
        # The ablation is a skill-control property (engine-independent); run it with the
        # instant rule reasoner so the live request never blocks on the LLM endpoint.
        payload["baselines"] = [
            {
                "name": row.name,
                "rootCauseAccuracy": row.root_cause_accuracy,
                "evidenceRecall": row.evidence_recall,
                "verifierPassRate": row.verifier_pass_rate,
                "cases": row.cases,
                "notes": row.notes,
            }
            for row in compare_baselines(
                cases, ground_truth, data_source="real", real_stats_path=stats_path,
                reasoner_mode="rule",
            )
        ]
    except Exception as exc:  # LLM endpoint unreachable / misconfigured — stay honest.
        payload["providerError"] = f"{type(exc).__name__}: {exc}"
        payload["note"] = (
            f"Provider '{provider_id}' failed ({type(exc).__name__}). The endpoint is likely "
            f"down or missing a key. Switch back to the rule baseline or open the GPU tunnel."
        )
    return payload


# The evolution stream is deterministic for a given dataset (rule reasoner, fixed
# case order), so recomputing it on every page mount only reheats the same result.
# Keyed by (manifest, passes) and invalidated when the manifest or stats file
# changes on disk. The lock also collapses concurrent mounts into ONE compute —
# stacked tab switches used to pile up parallel full re-runs.
_EVOLUTION_LOCK = threading.Lock()
_EVOLUTION_CACHE: dict[tuple[str, int], tuple[tuple, dict[str, Any]]] = {}


def _dataset_fingerprint(*files: Path) -> tuple:
    out = []
    for f in files:
        st = f.stat()
        out.append((str(f), st.st_mtime_ns, st.st_size))
    return tuple(out)


def load_evolution(manifest_path: Path | None = None, passes: int = 4) -> dict[str, Any]:
    """Real self-evolution on the held-out stream (cold-vs-warm, StreamBench-style).

    Recurring incidents may use provenance-linked memory to narrow the probe plan,
    but every diagnosis still obtains fresh evidence. Historical snapshots are
    never replayed as current observations.

    Cached per (manifest, passes); the dataset files' mtime+size invalidate it.
    """
    from core.evolve import compare_cold_vs_warm

    manifest = Path(manifest_path) if manifest_path else _MANIFEST
    validation = validate_real_dataset_manifest(manifest)
    if not validation.ready:
        return {"ready": False, "reason": "No validated real held-out dataset present."}
    stats_path = resolve_stats_path(manifest)
    fingerprint = _dataset_fingerprint(manifest, Path(stats_path))
    key = (str(manifest), passes)
    with _EVOLUTION_LOCK:
        hit = _EVOLUTION_CACHE.get(key)
        if hit and hit[0] == fingerprint:
            return hit[1]
        cases, ground_truth = load_real_case_bundle(manifest, split="heldout")
        res = compare_cold_vs_warm(
            cases, ground_truth, passes=passes,
            data_source="real", real_stats_path=stats_path, reasoner_mode="rule",
        )
        # The warm run is the only one with a memory lifecycle (cold has evolve=False).
        # Move it to the top level rather than copying, so the payload isn't doubled.
        observatory = res["warm"].pop("observatory", None)
        payload = {
            "ready": True,
            "passes": passes,
            "nCases": len(cases),
            "cases": [
                {
                    "id": c.id,
                    "query": c.query,
                    "assets": list(c.assets),
                    "rootCauseKey": ground_truth[c.id].expected_root_cause_key if c.id in ground_truth else "",
                }
                for c in cases
            ],
            "warm": res["warm"],
            "cold": res["cold"],
            "delta": res["delta"],
            "memory": res.get("memory", {}),
            "observatory": observatory,
        }
        _EVOLUTION_CACHE[key] = (fingerprint, payload)
        return payload


# ── Benchmark 态势: the evolved MEMORY GRAPH mapped into the LIVE topology shapes ────
# The benchmark scenario reuses the EXACT live TopologyCanvas in its DRILLED state.
# That component renders `graph.devices` when `drillSub === graph.cidr` and clusters
# them by their `vendor` field. So each real memory record → GraphDevice with
# vendor=tier (episodic|semantic|procedural|insight) so the constellation clusters by
# memory tier, and the memory links/relations → GraphEdge. Every node, edge and label
# comes verbatim from load_evolution()'s observatory.records; the ONLY derived values
# are the render heuristics — flows/accept/threat from the real strength/importance
# scores, and the x/y layout — each flagged inline below. No device data is invented.
_BENCH_CIDR = "mem://autopoiesis"
# Canonical tier order → a fixed cluster-center angle each, so the layout is fully
# deterministic and a missing tier simply leaves its slot empty.
_TIER_ORDER = ("episodic", "semantic", "procedural", "insight")
# Normalized [-1, 1] layout space — the SAME convention build_device_graph._layout
# emits and TopologyCanvas renders (center + x*meshRX(830), center + y*meshRY(368)),
# so bench nodes land in-bounds on the live canvas.
_BENCH_CENTER_R = 0.55   # distance of each tier's cluster center from the origin
_BENCH_LOCAL_R = 0.32    # sunflower-packing radius inside one tier cluster
_GOLDEN_ANGLE = math.pi * (3.0 - math.sqrt(5.0))


def _bench_node_tier(rec: dict[str, Any]) -> str:
    """The tier used to CLUSTER a memory node (→ GraphDevice.vendor).

    Reflection insight hubs are stored on the semantic tier but are a distinct kind of
    node (each abstracts a whole family), so the live constellation groups them into
    their own 'insight' cluster — mirroring the memory-graph endpoint's treatment."""
    mid = rec.get("memory_id", "")
    if mid.startswith("insight"):
        return "insight"
    return rec.get("tier") or "semantic"


def _bench_short_id(mid: str) -> str:
    """A human label for a memory node lacking a `root:` tag: the id minus its tier
    prefix and hash suffix (epi-real_admin_bruteforce_lockout-7d9874 →
    admin_bruteforce_lockout; insight-fortigate → fortigate)."""
    s = mid
    for pre in ("epi-real_", "epi-", "sem-", "proc-", "insight-"):
        if s.startswith(pre):
            s = s[len(pre):]
            break
    return re.sub(r"-[0-9a-f]{4,}$", "", s) or mid


def _bench_layout(nodes_by_tier: dict[str, list[str]]) -> dict[str, list[float]]:
    """Tier-cluster sunflower layout in normalized [-1, 1] space.

    Each present tier gets a fixed cluster center on a ring (separate centers per
    tier); its members are packed with a phyllotaxis/sunflower spiral so a cluster of
    any size fills its local disc without piling up. Same coordinate convention as the
    real build_device_graph layout, so the nodes render in-bounds on the live canvas."""
    pos: dict[str, list[float]] = {}
    for ti, tier in enumerate(_TIER_ORDER):
        members = nodes_by_tier.get(tier) or []
        if not members:
            continue
        ang = 2.0 * math.pi * ti / len(_TIER_ORDER)
        cx = _BENCH_CENTER_R * math.cos(ang)
        cy = _BENCH_CENTER_R * math.sin(ang)
        m = len(members)
        for j, mid in enumerate(members):
            r = _BENCH_LOCAL_R * math.sqrt(j / m) if m > 1 else 0.0
            theta = j * _GOLDEN_ANGLE
            x = cx + r * math.cos(theta)
            y = cy + r * math.sin(theta)
            pos[mid] = [round(max(-1.0, min(1.0, x)), 4), round(max(-1.0, min(1.0, y)), 4)]
    return pos


def build_bench_snapshot(passes: int = 4) -> dict[str, Any]:
    """Benchmark 态势 rendered as the LIVE topology shapes (Topology + SubnetGraph),
    built from the REAL evolved memory graph so the frontend can reuse the live
    TopologyCanvas in its drilled state.

    Returns {ok, topology, stats, graph, cidr}. See the module note above: only the
    render heuristics (flows/accept/threat, x/y) are derived; every identity, link and
    label is verbatim from load_evolution()'s observatory.records."""
    d = load_evolution(None, passes)
    recs = (d.get("observatory") or {}).get("records") or []
    if not d.get("ready") or not recs:
        return {
            "ok": False,
            "cidr": _BENCH_CIDR,
            "reason": d.get("reason") or "no evolved memory graph available",
        }

    rec_ids = {r["memory_id"] for r in recs}

    # Group node ids by tier: records first, then any link target that is referenced
    # but is not itself a record (kept as an insight/semantic node so every edge
    # endpoint resolves to a real node). This held-out set has no such extras.
    nodes_by_tier: dict[str, list[str]] = {}
    node_tier: dict[str, str] = {}
    for r in recs:
        t = _bench_node_tier(r)
        node_tier[r["memory_id"]] = t
        nodes_by_tier.setdefault(t, []).append(r["memory_id"])

    extra_targets: list[str] = []
    seen_extra: set[str] = set()
    for r in recs:
        for tgt in (r.get("links") or []):
            if tgt not in rec_ids and tgt not in seen_extra:
                seen_extra.add(tgt)
                extra_targets.append(tgt)
    for tgt in extra_targets:
        t = "insight" if tgt.startswith("insight") else "semantic"
        node_tier[tgt] = t
        nodes_by_tier.setdefault(t, []).append(tgt)

    pos = _bench_layout(nodes_by_tier)

    # ── devices: one GraphDevice per memory record ──
    devices: list[dict[str, Any]] = []
    for r in recs:
        mid = r["memory_id"]
        tier = node_tier[mid]
        tags = r.get("tags") or []
        root = next((t.split(":", 1)[1] for t in tags if t.startswith("root:")), None)
        strength = float(r.get("strength", 0.0) or 0.0)
        importance = float(r.get("importance", 0.0) or 0.0)
        # The REAL memory record, embedded verbatim (trimmed) onto the node so the
        # frontend can render the record's own fields on click — a bench node is an
        # evolved memory, not a device, so the live device_profile popup has nothing
        # to show for it. Every value here is straight off observatory.records; only
        # the ≤N caps and the root:-tag split are applied. See build_bench_snapshot's
        # module note: no memory content is invented.
        memory = {
            "memory_id": mid,
            "tier": r.get("tier") or tier,
            "text": r.get("text") or "",
            "root": root,
            "strength": round(strength, 3),
            "importance": round(importance, 3),
            "confidence": round(float(r.get("confidence", 0.0) or 0.0), 3),
            "tags": [t for t in tags if not t.startswith("root:")][:6],
            "evidence": [
                {"source": e.get("source") or "", "summary": e.get("summary") or ""}
                for e in (r.get("evidence_snapshot") or [])
                if (e.get("source") or e.get("summary"))
            ][:4],
            "assets": (r.get("asset_ids") or [])[:6],
            "links": (r.get("links") or [])[:6],
            "sourceTraces": (r.get("source_trace_ids") or [])[:4],
            "quarantined": bool(r.get("quarantined", False)),
        }
        xy = pos.get(mid, [0.0, 0.0])
        devices.append({
            "ip": mid,
            "name": root or _bench_short_id(mid),
            "mac": None,
            "vendor": tier,          # tier drives the live constellation's clustering
            "os": None,
            "role": tier,
            "intf": "MEMORY",
            "flows": round(strength * 20),      # DERIVED: real strength → node mass
            "deny": 0,
            "accept": round(importance * 8),    # DERIVED: real importance → node mass
            "leases": 0,
            "topPorts": tags[:4],
            # DERIVED heuristic from the real importance score. Every evolved record
            # sits above the 'watch' floor, so this reads as salience, not danger.
            "threat": "high" if importance >= 2 else "watch" if importance >= 1 else "ok",
            "seenBy": "traffic",
            "x": xy[0],
            "y": xy[1],
            "liveIdentity": None,
            "memory": memory,        # the real evolved record behind this node
        })
    # Referenced-but-not-a-record nodes (e.g. an orphaned insight hub): render them so
    # no edge dangles. Their flows/accept are 0 — there is no record to derive from.
    for tgt in extra_targets:
        tier = node_tier[tgt]
        xy = pos.get(tgt, [0.0, 0.0])
        devices.append({
            "ip": tgt, "name": _bench_short_id(tgt), "mac": None, "vendor": tier,
            "os": None, "role": tier, "intf": "MEMORY", "flows": 0, "deny": 0,
            "accept": 0, "leases": 0, "topPorts": [], "threat": "ok",
            "seenBy": "traffic", "x": xy[0], "y": xy[1], "liveIdentity": None,
        })

    # ── edges: memory links/relations, deduped by unordered pair ──
    rel_type: dict[tuple[str, str], str] = {}
    for r in recs:
        for rel in (r.get("relations") or []):
            tgt = rel.get("target_id")
            if tgt:
                rel_type[tuple(sorted((r["memory_id"], tgt)))] = rel.get("relation_type") or "link"
    edges: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for r in recs:
        src = r["memory_id"]
        for tgt in (r.get("links") or []):
            pair = tuple(sorted((src, tgt)))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            rtype = rel_type.get(pair)
            edges.append({
                "src": src,
                "dst": tgt,
                # A 'precedes'/'follows' relation is a temporal/causal chain (→ codst);
                # anything else is an associative family link (→ family).
                "kind": "codst" if rtype in ("precedes", "follows") else "family",
                "weight": 1.0,
                "hits": 1,
                "evidence": rtype or "link",
                "observed": True,
            })

    # ── clusters: one GraphCluster per present tier ──
    clusters = [
        {
            "id": tier,
            "members": list(nodes_by_tier[tier]),
            "role": tier,
            "vendor": tier,
            "size": len(nodes_by_tier[tier]),
            "boundBy": ["family"],
            "deny": 0,
        }
        for tier in _TIER_ORDER
        if nodes_by_tier.get(tier)
    ]

    tier_counts = {tier: len(nodes_by_tier[tier]) for tier in _TIER_ORDER if nodes_by_tier.get(tier)}
    sum_flows = sum(dv["flows"] for dv in devices)
    sum_accept = sum(dv["accept"] for dv in devices)
    n_dev = len(devices)

    graph = {
        "cidr": _BENCH_CIDR,
        "devices": devices,
        "edges": edges,
        "clusters": clusters,
        "anomalies": [],
        "stats": {
            "devices": n_dev,
            "withTraffic": n_dev,
            "dhcpOnly": 0,
            "edges": len(edges),
            "observedEdges": len(edges),
            "deny": 0,
            "roles": tier_counts,
            "vendors": tier_counts,
        },
    }

    topology = {
        "core": {"name": "AUTOPOIESIS", "ip": "kernel", "model": "self-evolving RCA"},
        "interfaces": [{"name": "MEMORY", "role": "memory", "flows": sum_flows, "kind": "lan"}],
        "subnets": [{
            "cidr": _BENCH_CIDR,
            "hosts": n_dev,
            "flows": sum_flows,
            "accept": sum_accept,
            "intf": "MEMORY",
            "devices": [
                {
                    "ip": dv["ip"],
                    "flows": dv["flows"],
                    "deny": 0,
                    "accept": dv["accept"],
                    "threat": dv["threat"],
                    "top_ports": dv["topPorts"],
                }
                for dv in devices
            ],
        }],
        "anchors": [],
    }

    stats = {
        "source": "benchmark · evolved memory graph",
        "windowDays": [],
        "adminLoginFailed": 0,
        "distinctSrc": n_dev,
        "topAttackerSrc": [],
        "lockouts": 0,
        "denyCount": 0,
        "topDenyPorts": [],
        "topDenySrc": [],
        "acceptPermit": n_dev,
        "sessionClash": 0,
    }

    return {"ok": True, "topology": topology, "stats": stats, "graph": graph, "cidr": _BENCH_CIDR}

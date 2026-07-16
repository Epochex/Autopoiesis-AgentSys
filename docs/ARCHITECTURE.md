# Architecture

Autopoiesis-AgentSys is a memory–context–skill self-evolution kernel for long-running
agents. The unit of truth is the **trace**: every task produces an append-only record of
memory reads, context choices, skill exposure, tool calls, verifier outcomes, cost, and
failure modes. The system learns from those traces **offline**, without making human
review the dominant signal.

> **Scope.** The measured system is the Python kernel in [`core/`](../core) +
> [`domains/`](../domains). An earlier TypeScript prototype (a `src/` tree with an `npm`
> workflow) has been **removed**; it is not referenced anywhere below.

## Online path — a single read-only agent

```text
alert / task
  → tiered memory retrieve            (core/memory)
  → episodic recall shortcut          (reuse a provenance-linked evidence snapshot on recurrence)
    ── or ── probe read-only skills    (attention-selected shortlist)
  → evidence-aware context compile     (core/context/compiler.py, to a token budget)
  → reasoner                           (domains/network_rca/reasoner.py — rules by default; optional DeepSeek LLM)
  → citation verifier                  (core/verifier/verifier.py)
  → append typed events                (core/trace/ledger.py — 22-kind closed vocabulary)
```

`SingleAgentRCAOrchestrator.diagnose()` ([`core/orchestrator/orchestrator.py`](../core/orchestrator/orchestrator.py))
is the shipped online entry point. It is deliberately narrow: it exposes only a small,
task-relevant skill shortlist and only clean, high-confidence memories. Write-capable
skills are **hard-blocked** with defense in depth — a relevance filter, a spec-level
check, and a result-level check (a skill that lies about being read-only is still caught).

## Skill attention — `core/skills`

Skill overload is a first-class failure mode. `SkillAttentionController.select`
([`controller.py`](../core/skills/controller.py)) is a hard **relevance gate** — a skill
is eligible only if it is *preferred* or *tag-matched* to the query — with learned
ranking **inside** the relevant set:

```text
score = topical_relevance + success_rate − 2·misuse_rate − 0.05·cost      (top-k)
```

A globally "good" skill can never widen its own scope. `SkillSpec` carries
`success_count`, `misuse_count`, `cost`, `frozen`, and `risk`.
[`induction.py`](../core/skills/induction.py) synthesizes a new skill from recurring trace
signals and promotes it only through a **replay gate** (golden non-regression + handler
replay).

## Memory model — `core/memory` + `core/evolve`

Three tiers (CoALA): **episodic** (what happened, with an evidence snapshot), **semantic**
(stable facts / reflected insights), **procedural** (reusable patterns tagged by root
cause / skill). This is not plain RAG — memory is a *lifecycle*:

| mechanism | paper | code | status |
|---|---|---|---|
| write routing ADD / UPDATE / NOOP | Mem0 | `evolve/memory_ops.py` | ✅ wired |
| associative links (top-k, bidirectional) | A-MEM | `evolve/memory_ops.py` | ✅ wired |
| importance-gated reflection → insight | Generative Agents | `evolve/memory_ops.py` | ✅ wired |
| decay / forgetting below a floor | Ebbinghaus | `evolve/memory_ops.py` | ⚠️ implemented + unit-tested, **not yet wired into the loop** |
| quarantine (kept for audit, excluded from retrieval) | — | `memory/store.py` | ✅ wired |

A topology-graph / logical-retrieval variant
([`topo_graph.py`](../core/memory/topo_graph.py),
[`logical_retrieval.py`](../core/memory/logical_retrieval.py)) is used by an eval harness
(`core/eval/retrieval_precision.py`), **not** the online diagnose path (which uses the
simpler tiered retrieve).

## Background evolution — `core/evolve`

Between events, `consolidate_run` turns a trace into memory notes (routing + links +
reflection + skill-stat updates), quarantining traces with unsupported claims or verifier
failures. `compare_cold_vs_warm` / `run_evolving_stream` measure the online path getting
**cheaper on a recurring stream** (StreamBench-style; the recalled snapshot is re-run
through the reasoner + verifier, so caching cannot trade correctness for speed).
[`grpo.py`](../core/evolve/grpo.py) exports group-relative advantages and a
confidence-update rule — **roadmap: not wired into the online loop, and not LLM/GPU policy
training.**

## Verification — `core/verifier`

- **Citation verifier** — every cited fact must have been observed in evidence; also
  catches contradictions and missing *required* evidence.
- **Contract verifier** ([`contracts.py`](../core/verifier/contracts.py)) — pre/post/
  invariant conditions plus a grounded read-back, with rollback on violation.

## Adaptive escalation (opt-in) — `core/orchestrator`

`AdaptiveOrchestrator` wraps the base agent with a read-only **planner / executor /
critic** ([`agents.py`](../core/orchestrator/agents.py)) and a cascading **intent router**
([`intent_router.py`](../core/orchestrator/intent_router.py)). It runs the base single
agent first and escalates only on ambiguity or blast-radius triggers, preserving the
read-only gate at every layer.

> **Honest note.** This multi-agent path is **opt-in and currently exercised by tests, not
> the deployed console.** The shipped online path is the single agent above — so "single
> agent online" is a configuration the console uses, not an architectural guarantee of the
> whole repo.

## Evaluation — `core/eval` + `domains/*/eval*.py`

- **Replay ablation** over four configs (full · − compression · − memory · − skill
  scheduling) on the real held-out set.
- **Real FortiGate held-out** (6 curated real-log incident types) with a deterministic
  rule reasoner — an engine-independent baseline that rules out an LLM confound.
- **LongMemEval-style** LLM-free recall@k harness ([`core/eval/longmemeval.py`](../core/eval/longmemeval.py)).
- **Domains**: `network_rca` (real Dahua FortiGate syslog — the headline), `active_recon`
  (self-pentest over a labeled RFC-5737 **mock** target), `enterprise_ops` (contract-checked
  anomaly **simulation** on synthetic data).

`python3 -m pytest tests_py/ -q` → **125 tests**.

## Research backbone

CoALA (2309.02427) · Mem0 (2504.19413) · A-MEM (2502.12110) · Generative Agents
(2304.03442) · Voyager (2305.16291) · StreamBench (2406.08747) · LongMemEval (2410.10813)
· GRPO / DeepSeekMath (2402.03300, roadmap) · Ebbinghaus (1885). Full method and verified
citation list in [BENCHMARKS.md](./BENCHMARKS.md).

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
  → BM25 / asset / graph retrieve; optional Flat dense route (core/memory)
  → episodic hypothesis               (historical evidence is provenance, never current state)
  → hybrid knowledge-document retrieve (BM25 + exact Flat + RRF; optional evaluated reranker)
  → probe read-only skills             (fresh evidence; procedural memory may narrow the shortlist)
  → evidence-aware context compile     (core/context/compiler.py, to a token budget)
  → reasoner                           (domains/network_rca/reasoner.py — rules by default; optional DeepSeek LLM)
  → citation verifier                  (core/verifier/verifier.py)
  → append typed events                (core/trace/ledger.py — closed vocabulary)
```

`EvolvingRCAService.diagnose()` is the long-lived service entry point. It serializes each
request because the underlying orchestrator carries run-local trace state, catches up the
durable memory event stream before reasoning, and consolidates only verifier-approved runs.
`SingleAgentRCAOrchestrator.diagnose()` remains the immutable evaluation primitive. The path exposes only a small,
task-relevant skill shortlist and only clean, high-confidence memories. Write-capable
skills are **hard-blocked** with defense in depth — a relevance filter, a spec-level
check, and a result-level check (a skill that lies about being read-only is still caught).

The service also emits a separate parent/child node trajectory for execution diagnosis.
It groups one run by `trace_id`, connects repeated runs through `session_id`, and records
retrieval, tools, context, reasoning, verification, memory writes, index maintenance and
business-ledger persistence. Local replay is authoritative; optional Langfuse export runs
through a bounded background queue. See [EXECUTION_OBSERVABILITY.md](./EXECUTION_OBSERVABILITY.md).

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
[`induction.py`](../core/skills/induction.py) can capture and replay-gate a routing candidate.
Its generated handler is not a new executable business capability, so production promotion
still requires an approved handler implementation.

## Memory model — `core/memory` + `core/evolve`

Three tiers (CoALA): **episodic** (what happened, with an evidence snapshot), **semantic**
(stable facts / reflected insights), **procedural** (reusable patterns tagged by root
cause / skill). This is not plain RAG — memory is a *lifecycle*:

| mechanism | paper | code | status |
|---|---|---|---|
| write routing ADD / UPDATE / NOOP | Mem0 | `evolve/memory_ops.py` | ✅ wired |
| associative links plus bounded multi-hop expansion | A-MEM | `evolve/memory_ops.py`, `memory/store.py` | ✅ wired |
| typed temporal/causal event-chain reconstruction | trace relations | `memory/evolution.py` | ✅ wired; causal edges require evidence |
| importance-gated reflection → insight | Generative Agents | `evolve/memory_ops.py` | ✅ wired |
| decay / forgetting below a floor | Ebbinghaus | `evolve/memory_ops.py` | ⚠️ baseline implemented and tested, not wired into the loop |
| capacity-budgeted utility eviction | lifecycle signals | `evolve/memory_ops.py` | ✅ wired into the evolving stream |
| quarantine (kept for audit, excluded from retrieval) | — | `memory/store.py` | ✅ wired |

Memory retrieval no longer constructs a complete BM25 index inside each request.
`SegmentedBM25Index` maintains a mutable delta, immutable sealed segments, global corpus
statistics, document versions and tombstones. `TieredMemoryStore.add`, `reindex`, and
`quarantine` update that index synchronously, so a successful write is immediately visible.
Compaction builds outside the mutation lock and swaps a complete generation under a short
lock. `IndexMaintenanceWorker` performs threshold checks outside request handling and exposes
attempt, abort, failure and duration counters.

When `AUTOPOIESIS_MEMORY_DSN` is configured, `PostgresMemoryRepository` is the durable source
of truth. A current-state row and a full-snapshot event are committed in one transaction;
per-record versions reject lost updates, a database trigger makes events append-only, and
monotonic consumer checkpoints cannot pass the committed high-water mark. Startup restores
all records and rebuilds the local indexes. `MemoryIndexProjector` consumes events in offset
order and advances its checkpoint only after BM25, assets and the enabled vector route accept the event. Store
flushes carry loaded versions, so concurrent processes cannot silently overwrite one another;
a failed flush reloads the durable snapshot before another request is served. Without a DSN,
the deterministic test/default mode remains in process.

Online semantic memory retrieval is optional and disabled by default. Setting
`AUTOPOIESIS_ENABLE_VECTOR_MEMORY=1` enables the same lifecycle boundary through
`VectorIndexLifecycle`: an immutable FAISS Flat base by default, exact Flat delta,
version-filtered merged search, checksummed snapshots and an atomic `CURRENT` pointer.
HNSW remains an explicit deployment alternative; it is rebuilt as a complete candidate
generation rather than modified in place. Full research rationale and churn results are in
[`INDEX_LIFECYCLE_RESEARCH.md`](./INDEX_LIFECYCLE_RESEARCH.md).

The optional online knowledge-document route is injected through
`build_network_rca_orchestrator(knowledge_retriever=...)` or constructed from a corpus path.
Its BM25 and exact-Flat candidates are fused with RRF, while Cross-Encoder reranking is off
by default. Retrieved documents enter the compiled context and verification input as
`knowledge_document` evidence, but the verifier requires at least one cited current
operational observation before a diagnosis can pass.

A topology-graph / logical-retrieval variant
([`topo_graph.py`](../core/memory/topo_graph.py),
[`logical_retrieval.py`](../core/memory/logical_retrieval.py)) is used by an eval harness
(`core/eval/retrieval_precision.py`), **not** the online diagnose path (which uses the
simpler tiered retrieve).

## Background evolution — `core/evolve`

After a verified run, `consolidate_run` turns attributed trace facts into memory notes
(routing + links + reflection + skill-stat updates). Merely retrieved memories receive no
credit or blame. Only an explicitly attributed episodic hypothesis can accumulate a
contradiction strike, and two independent runs with cited fresh counter-evidence are required
before quarantine. `compare_cold_vs_warm` / `run_evolving_stream` remain evaluation drivers.
[`grpo.py`](../core/evolve/grpo.py) exports group-relative advantages and a
confidence-update rule — **roadmap: not wired into the online loop, and not LLM/GPU policy
training.**

## Verification — `core/verifier`

- **Citation verifier** — every cited fact must have been observed; root-cause-specific
  evidence contracts reject citations that exist but do not support the selected claim.
- **Contract verifier** ([`contracts.py`](../core/verifier/contracts.py)) — pre/post/
  invariant conditions plus grounded read-back. Preconditions and single-use human approval
  are checked before a handler; failed writes are compensated and the restored state is read
  back, otherwise manual recovery is explicit.

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

The default, FAISS-enabled and PostgreSQL 17 suites are run separately; current counts are
recorded in the repository claim bank after each release rather than frozen in this document.

## Research backbone

CoALA (2309.02427) · Mem0 (2504.19413) · A-MEM (2502.12110) · Generative Agents
(2304.03442) · Voyager (2305.16291) · StreamBench (2406.08747) · LongMemEval (2410.10813)
· GRPO / DeepSeekMath (2402.03300, roadmap) · Ebbinghaus (1885). Full method and verified
citation list in [BENCHMARKS.md](./BENCHMARKS.md).

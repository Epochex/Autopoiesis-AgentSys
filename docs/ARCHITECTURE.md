# Architecture

`selfevo-orchiter` is a memory-context-skill self-evolution kernel for long-running agents. The important object is the trace: every task produces a record of memory reads, context choices, skill exposure, tool calls, verifier outcomes, token cost, and failure modes. The system learns from those traces without making human review the dominant signal.

## Online Path

```text
AgentTask
  -> memory query planner
  -> context compiler
  -> skill attention controller
  -> agent execution
  -> automatic verifier
  -> trace ledger
```

The online path is deliberately narrow. It should not expose every memory and every skill to the model. `src/skill-os/skillAttentionController.ts` selects a small skill shortlist from task tags, skill descriptions, performance statistics, wrong-invocation rate, bypass rate, token cost, latency, and risk. `src/memory-os/temporalRetriever.ts` retrieves clean episodic, semantic, and procedural notes while excluding contaminated memories by default.

## Background Evolution Path

```text
TraceLedger
  -> MemoryConsolidator
  -> ReflectionSummarizer
  -> SkillAttention feedback update
  -> GRPO group reward export
  -> Replay / verifier promotion gate
```

`MemoryConsolidator` turns traces into memory notes. It stores clean notes and quarantines traces with unsupported claims, unsafe actions, or verifier failures. `summarizeTraceLessons` extracts reusable lessons only from high-confidence trace steps. `evaluateLessonPromotion` requires replay cases, positive reward delta, high verifier pass rate, and no regressions.

## Memory Model

The system separates three memory tiers:

- Episodic memory records what happened in a specific trace.
- Semantic memory records stable task context and domain facts.
- Procedural memory records reusable patterns such as how to repair context, when to retrieve memory, or how to avoid stale evidence.

This is not plain RAG. RAG remains useful for semantic retrieval, but the project treats memory as a lifecycle with write-time consolidation, contamination detection, retrieval-time scoring, and replay-backed promotion.

## Skill Attention

Skill overload is treated as a first-class failure mode. Every skill has performance statistics:

```text
attempts
successes
wrong_invocations
bypasses
unsafe_blocks
total_token_cost
total_latency_ms
last_success_at
```

The controller rewards tag/term matches and historical success. It penalizes wrong invocation, silent bypass, cost, latency, and risky permissions. The output is not a policy slogan; it is a concrete top-k skill shortlist plus an irrelevant-exposure reduction estimate.

## GRPO Boundary

`src/training/grpoDataset.ts` exports group-relative training samples. A group contains multiple rollouts for the same objective. Each rollout keeps the trace, action summary, step rewards, total reward, and advantage relative to the group mean.

The intended trainable policies are:

- memory retrieval policy
- context compression policy
- skill attention policy
- stop / repair policy

The TypeScript repo owns trace capture, reward construction, and dataset export. GPU-side GRPO / LoRA training can live in a separate training stack because the mature tooling is Python-based.

## Evaluation

Current tests verify five automatic properties:

- clean procedural memory ranks ahead of contaminated memory under a procedural query
- skill attention keeps required skills visible while reducing irrelevant exposure
- wrong tool feedback demotes a skill without human intervention
- reflection lessons require replay-backed promotion
- GRPO export produces positive and negative advantages across rollouts

The next benchmark layer should map these same metrics onto LongMemEval-style memory tasks, WorkArena-style enterprise workflows, and tool-selection suites.

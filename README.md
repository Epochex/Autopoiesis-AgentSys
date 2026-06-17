# selfevo-orchiter

`selfevo-orchiter` is a TypeScript-first self-evolution kernel for long-running agents. Its core problem is not generic orchestration. It focuses on the failure mode that appears after agents run for weeks: memory becomes stale or polluted, context grows without discipline, too many skills distract the model, and useful experience never becomes a stable policy.

The online path stays small. The background path learns.

```text
task
  -> memory query planner
  -> context compiler
  -> skill attention controller
  -> single-agent execution
  -> automatic verifier
  -> trace ledger

trace ledger
  -> memory consolidation
  -> reflection summarization
  -> skill scoring and pruning
  -> GRPO group reward export
  -> replay promotion gate
```

## Core Thesis

RAG is a retrieval primitive, not the system boundary. This repo treats memory as a lifecycle: raw traces are preserved, clean experience is consolidated into episodic / semantic / procedural notes, contaminated memories are quarantined, context is compiled under a budget, and only a small task-relevant skill shortlist is exposed to the agent.

## What This Repo Implements

- `src/memory-os/`: trace-to-memory consolidation, contamination reports, tiered memory retrieval, procedural memory prioritization.
- `src/skill-os/`: skill attention control, top-k exposure, wrong-tool demotion, bypass and risk accounting.
- `src/reflection/`: automatic trace lesson extraction and replay-backed promotion gates.
- `src/training/`: GRPO-style group construction with rollout rewards and advantages.
- `src/evolution/`: evidence-aware context packets, memory graph, topology gate, reward shaping, policy candidates.
- `src/core/`, `src/skills/`, `src/harness/`: kernel, typed skill SDK, replay tests, regression gates, and trace infrastructure.

Domain adapters such as `office`, `netops`, `coding`, and `decision` are stress tests. They are not the project identity.

## Why This Is Not Just RAG

```text
RAG:
query -> retrieve chunks -> append to prompt

selfevo-orchiter:
trace -> memory note -> contamination check -> tiered retrieval
      -> context compilation -> skill shortlist -> verifier
      -> reflection -> promotion gate -> GRPO-ready group rewards
```

The target policies are concrete:

- Memory policy: when to retrieve, which tier to trust, when to quarantine stale or unsupported memories.
- Context policy: which evidence branches enter the prompt under token pressure.
- Skill attention policy: which skills are exposed, hidden, demoted, archived, or promoted.
- Reflection policy: which lessons are reusable and which are rejected before long-term memory pollution.

## Useful Commands

```bash
npm ci
npm run typecheck
npm test
node --test dist/tests/memorySkillEvolution.test.js
npm run harness:replay -- examples/suites/portable-work-items.json
npm run harness:gate -- /tmp/selfevo-report.json examples/policies/local-strict.json
```

Provider smoke testing is opt-in:

```bash
npm run provider:smoke
SELFEVO_RUN_PROVIDER_SMOKE=1 npm run test:provider
```

## Research Backbone

The implementation follows a practical synthesis of A-MEM-style dynamic memory organization, EvolveR-style experience lifecycle learning, SkillEvolver-style deployed-skill auditing, and GEPA / GRPO-style trace-level policy optimization.

- A-MEM: https://arxiv.org/abs/2502.12110
- EvolveR: https://arxiv.org/abs/2510.16079
- SkillEvolver: https://arxiv.org/abs/2605.10500
- GEPA: https://arxiv.org/abs/2507.19457

## Package Map

```text
src/memory-os/     Episodic / semantic / procedural memory lifecycle
src/skill-os/      Skill attention scoring, pruning, and feedback updates
src/reflection/    Trace lesson extraction and promotion gates
src/training/      GRPO group reward dataset export
src/evolution/     Context compiler, memory graph, topology gate, reward, policy lab
src/core/          Agent kernel and lifecycle contracts
src/skills/        Typed skill SDK and built-in skills
src/harness/       Replay, matrix profiles, aggregate metrics, regression gates
src/context/       Legacy scoped memory stores and context packs
src/domains/       Portable stress-test adapters, not the headline system
```

## Boundary

The system does not let agents freely rewrite production behavior. It optimizes memory retrieval, context compression, skill exposure, reflection promotion, and GRPO-ready policy datasets. Candidate improvements must pass verifier and replay gates before becoming active.

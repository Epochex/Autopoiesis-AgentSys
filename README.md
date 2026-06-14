# selfevo-orchiter

`selfevo-orchiter` is a TypeScript-first platform for self-evolving agent orchestration. It is not a chatbot demo and it is not a Python research artifact. The project focuses on the infrastructure behind enterprise agents: durable orchestration, scoped memory, auditable context compression, multi-agent exception paths, verifier gates, replay evaluation, and GRPO-style offline policy iteration.

The core idea is simple: online agents should stay bounded and auditable; offline agent teams can be aggressive when they mine failures, propose policy candidates, and run replay gates.

```text
enterprise work item / trace
  -> AgentWorkItem contract
  -> memory retrieval
  -> context packet compiler
  -> single orchestrator by default
  -> specialist topology only for hard cases
  -> verifier / reviewer
  -> trace ledger
  -> offline reward + policy candidate
  -> release gate
```

## What This Repo Implements

- TypeScript agent kernel with checkpoints, durable events, repair, cancellation, approval gates, and resumability.
- Typed skill infrastructure for memory, policy checks, document drafting, workspace search, decision simulation, and sandboxed CLI execution.
- Enterprise memory stores and bounded context packs.
- New `src/evolution/` layer for memory graphs, branch-preserving context packets, specialist topology gates, step rewards, and policy candidates.
- Domain-neutral `AgentWorkItem` inputs spanning coding, office workflows, decision simulation, and high-stakes investigation.
- Replay harness, matrix profiles, regression gates, trace diffs, and CI validation.
- OpenAI-compatible provider adapter isolated from the kernel.

## Why TS-First

The platform is intentionally TypeScript-first because the target is production agent infrastructure: typed task contracts, web/service integration, durable event records, CLI tooling, replay fixtures, and refactor-safe orchestration. Historical Python projects are useful as research inputs, but this repo keeps the implementation surface in TS unless there is a strong reason not to.

## Evolution Layer

The new evolution layer maps recent agent-system research into concrete engineering primitives:

- `EnterpriseMemoryGraph`: stores cases, evidence, verifier failures, human decisions, policy candidates, and utility scores.
- `compileContextPacket`: emits selected / excluded / missing context surfaces under item and token budgets.
- `decideSpecialistTopology`: keeps the default path single-agent and enables star, critic-loop, or sparse-consensus only for hard cases.
- `scoreStepReward` and `redistributeTraceRewards`: provide GRPO-style step credit signals for context, memory, provider, stop, and escalation policies.
- `buildPolicyCandidate`: turns replay traces into release-gated candidates with evidence, safety status, and patch hints.

## Useful Commands

```bash
npm ci
npm run typecheck
npm test
npm run harness:replay -- examples/suites/portable-work-items.json
npm run harness:replay -- examples/suites/portable-work-items.json --out /tmp/selfevo-report.json
npm run harness:gate -- /tmp/selfevo-report.json examples/policies/local-strict.json
```

Provider smoke testing is opt-in:

```bash
npm run provider:smoke
SELFEVO_RUN_PROVIDER_SMOKE=1 npm run test:provider
```

The environment variable prefix still accepts `HELIX_*` for backward compatibility with older local scripts, but the repository identity is `selfevo-orchiter`.

## Package Map

```text
src/core/          Agent kernel, lifecycle contracts, plan validation
src/kernel/        Default system assembly for planner, skills, memory, sandbox, and reviewer
src/agents/        Planner, executor, reviewer modules
src/skills/        Skill SDK and built-in skills
src/context/       Memory stores and context pack construction
src/evolution/     Memory graph, context compiler, topology gate, reward, policy lab
src/harness/       Evaluation runner, matrix profiles, aggregate metrics, regression gates
src/persistence/   File checkpoints, JSONL events, durable run store
src/providers/     OpenAI-compatible model provider and smoke checks
src/sandbox/       Local subprocess sandbox
src/trace/         Trace export, redaction, and run-to-run diffs
src/domains/       Portable domain adapters used as stress tests
```

## Boundary

`selfevo-orchiter` optimizes orchestration policies, memory retrieval, context budgets, provider routing, specialist topology gates, and stop/escalation decisions. It does not claim that agents should freely modify production systems. Candidate patches stay behind replay, verifier, cost, safety, and human gates.

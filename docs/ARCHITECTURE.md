# Architecture

`selfevo-orchiter` is a TypeScript-first platform for long-running, evaluated, self-evolving agent systems. The online path is conservative: bounded context, typed skills, reviewer/verifier gates, durable traces, and explicit approvals. The offline path is where multi-agent iteration, reward shaping, and policy candidates live.

## Layers

```text
AgentWorkItem
  -> AgentTask
  -> planner
  -> bounded context / memory pack
  -> executor skill calls
  -> reviewer / verifier
  -> durable trace
  -> harness replay
  -> evolution policy candidate
```

### Kernel

`src/core/` owns durable orchestration: task contracts, plans, steps, checkpoints, repair, cancellation, approval gates, and lifecycle events. The kernel does not know about model vendors or domain-specific evidence formats.

### Skills

`src/skills/` defines typed skills with schemas, permissions, risk labels, observations, and approval policy. Built-ins include document drafting, memory search, policy checks, decision simulation, workspace search, and sandboxed CLI execution.

### Context and Memory

`src/context/` provides scoped memory records and bounded context packs. `src/evolution/memoryGraph.ts` extends that into a graph-shaped enterprise memory layer where cases, evidence, verifier findings, human decisions, policies, providers, and skills carry utility and provenance.

### Evolution Lab

`src/evolution/` is the self-evolving layer:

- memory graph retrieval with utility scoring
- branch-preserving context packet compilation
- multi-agent topology gate
- step-level reward redistribution
- release-gated policy candidate generation

This layer does not mutate production behavior directly. It emits candidates for replay and approval.

### Harness

`src/harness/` runs repeatable evaluation suites and regression gates. It keeps self-evolution honest by measuring completion, failures, approval requirements, latency, coverage, and scenario validity.

## Multi-Agent Policy

The default online topology is single orchestrator. Specialist topologies are activated only when signals justify the overhead:

- low branch coverage
- repeated verifier rejection
- provider disagreement
- memory conflict
- high risk with enough budget

Supported modes are `star`, `critic_loop`, and `sparse_consensus`. The point is not to add agents everywhere; the point is to make multi-agent coordination a measurable exception path.

## Reward Boundary

Offline rewards target policy choices:

```text
R = branch_coverage_gain
  + verifier_pass
  + human_acceptance
  + memory_reuse_success
  - unsupported_claim
  - missing_evidence
  - token_cost
  - latency_cost
  - unsafe_action
```

The system optimizes evidence/context selection, memory retrieval, provider routing, topology gating, and stop/escalation policy. It does not directly optimize unsafe action execution.

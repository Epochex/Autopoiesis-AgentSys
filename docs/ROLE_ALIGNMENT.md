# Project Identity

Autopoiesis-AgentSys is the current network situational-awareness and incident-investigation system for a private network with mixed devices. It continuously receives FortiGate and host observations, detects known conditions, groups related signals into cases, investigates unresolved causes with read-only tools, controls approved actions, reads recovery evidence back from the managed system, and retains validated incident knowledge for recurrence handling.

## Business boundary

The production chain owns four outcomes:

- turn a stream of device events into an asset-scoped incident with a stable `case_id`;
- resolve conditions that are not covered by a single fixed rule through evidence-driven investigation;
- carry a confirmed cause into a permitted action, an observation window and a recovery decision;
- reuse validated incident history when a compatible fault recurs, while keeping current observations authoritative.

NetOps is the frozen research and paper repository. Its historical experiments remain in the dated archive described by [`NETOPS_ASSET_SEPARATION.md`](./NETOPS_ASSET_SEPARATION.md). Autopoiesis production dependencies are limited to the current repository, `autopoiesis.*` topics, the `autopoiesis` ClickHouse database, the `autopoiesis_production` memory schema and `/data/autopoiesis-production` outputs.

## Engineering signals

| Capability | Concrete implementation |
|---|---|
| Persistent investigation | Case state, competing hypotheses, probe results and action observations survive service restarts |
| Context compilation | Exact ClickHouse queries, historical incident retrieval and knowledge retrieval are filtered by asset, time, source and validation state |
| Tool use | Read-only probes and approved actions use typed inputs, bounded timeouts, idempotency keys and explicit result objects |
| Memory | Incident dossiers, risk patterns, network features and execution records enter later retrieval through scoped repositories |
| Safety | Action eligibility, approval, blast radius, stop conditions and recovery readback are maintained by deterministic components |
| Evaluation | Fixed scripts, direct-model calls and the complete system run against the same case inputs and preserve raw traces |

The public project narrative follows this production boundary. Paper-specific classifiers, benchmark names and historical service identities remain in the frozen archive.

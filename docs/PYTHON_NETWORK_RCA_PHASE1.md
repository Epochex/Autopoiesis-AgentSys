# Python Network RCA Phase 1

This layer is a Python (3.10+) implementation of the Phase 0-1 contract for the Autopoiesis-AgentSys framework. (An earlier TypeScript kernel has since been removed; Python `core/` + `domains/` is the only implementation.) This package adds a runnable and testable RCA seed domain with the `core/` and `domains/network_rca/` layout.

## What Is Included

- Append-only JSONL trace ledger with replay support.
- Tiered memory store for episodic, semantic, procedural, and per-asset profile memory.
- Evidence-budgeted context compiler.
- Readonly skill registry and attention controller.
- Single-agent online orchestrator.
- Automatic verifier for evidence citation, readonly behavior, and required evidence recall.
- Network RCA seed domain with five deterministic mock cases.
- FortiOS key-value syslog parser and local fixture log adapter.
- Live device adapter and R230 ingestor client remain opt-in and are not used by tests.

Seed case ground truth is evaluator-only metadata. The online orchestrator receives only the case query, assets, query terms, and candidate domain skills; root cause labels and required evidence ids are read only by trace replay evaluation.

## Run

```bash
pytest -q tests_py
python3 -m domains.network_rca.demo
```

The demo writes a replayable trace to `artifacts/network_rca_phase1_trace.jsonl`.

## Safety Boundary

The default path uses `MockDeviceAdapter` and fixture logs only. Live FortiGate access is behind `SELFEVO_ENABLE_LIVE_DEVICE_ADAPTER=1`, and credentials are read from environment variables. No code path stores device credentials in source, prompt text, fixtures, or trace output.

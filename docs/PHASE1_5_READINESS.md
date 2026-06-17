# Phase 1.5 Readiness

Phase 1 proves only that the pipeline is wired. Its 1.0 mock metrics are not RCA-quality evidence because both the rules and fixtures are handwritten.

## Current Real-Data Status

- R230 `192.168.1.23:514` is reachable, so syslog receiving likely exists.
- No readonly R230 ingestor API was detected on `8000`, `8026`, `8080`, or `9090`.
- No 3-7 day real FortiGate syslog export was found locally under `/data`.
- The current `domains/network_rca/fixtures/fortios_syslog_samples.log` is explicitly mock/sample data, not a real evaluation fixture.

Run:

```bash
python3 -m domains.network_rca.phase15
```

The command prints `real_data_readiness.blocked=true` until real syslog fixtures and held-out ground truth exist.

## What Counts As Real For Phase 1.5

- 3-7 days of FortiGate syslog captured from R230 through a readonly export or ingestor.
- Train and held-out eval split stored separately.
- Human-labeled or independently validated ground truth for held-out cases.
- Baseline table over the same held-out cases: selfevo light path, full context, full tools, and no memory.

Mock baseline rows are allowed only as pipeline checks and must be labeled `dataset_kind=mock`.

## TypeScript Boundary

This repository started as a TypeScript self-evolution kernel. The Python `core/` and `domains/network_rca/` tree is the implementation surface for the Python-only Phase 0-1 RCA spec. Existing TypeScript modules remain as legacy/reference material and frontend-adjacent tooling; Python Phase 1.5 metrics must not depend on them.

## CI Boundary

A Python CI workflow should run on stable Python 3.11.x. It is not currently committed because the available GitHub token previously lacked `workflow` scope and GitHub rejected pushes containing `.github/workflows/*`.

# Phase 1.5 Readiness

Phase 1 proves only that the pipeline is wired. Its 1.0 mock metrics are not RCA-quality evidence because both the rules and fixtures are handwritten.

## Current Real-Data Status (2026-06-17: real data wired)

- R230 (`192.168.1.23`) receives real `DAHUA_FORTIGATE` (FG100E) syslog via rsyslog
  (`/etc/rsyslog.d/30-fortigate.conf`, facility `local7` from `192.168.1.1`). The logs land at
  `/data/fortigate-runtime/input/fortigate.log` (NOT `/var/log/fortigate/` — the earlier probe
  looked in the wrong place, which is why it reported `blocked` for the wrong reason).
- A real held-out dataset now exists locally at `domains/network_rca/fixtures/real/`
  (manifest + train/heldout cases + authoritative `real_window_stats.json` computed over the full
  R230 capture). These files are gitignored: they contain internal/external IPs and are not committed.
- Readiness is no longer gated on a (nonexistent) ingestor port. `probe_r230_readiness` is
  `blocked` iff no validated real dataset is present. The optional R230 ingestor is just one way to
  fetch logs; a local export is sufficient.

### Real held-out baseline result (rule reasoner, deterministic baseline)

`python3 -m domains.network_rca.eval_real_heldout domains/network_rca/fixtures/real/manifest.json`

| baseline | root-cause acc | evidence recall |
|---|---|---|
| selfevo_light_path | 1.00 | 1.00 |
| full_context | 1.00 | 1.00 |
| full_tools (no skill control) | **0.50** | **0.50** |
| no_memory | 1.00 | 1.00 |

The informative signal is the **full_tools degradation**: removing the skill controller exposes every
skill, so the window's dominant brute-force evidence swamps the deny case and it is misdiagnosed. The
1.00 rows are a deterministic rule baseline on real data — NOT a proof of RCA reasoning quality.
Real reasoning-quality numbers require the LLM reasoner against a configured endpoint (pending).

Run:

```bash
python3 -m domains.network_rca.phase15
```

The command prints `real_data_readiness.blocked=true` until real syslog fixtures and held-out ground truth exist.

Validate a real dataset manifest with:

```bash
SELFEVO_REAL_DATASET_MANIFEST=/path/to/manifest.json python3 -m domains.network_rca.validate_real_dataset
```

Run real held-out baselines only after the manifest validates:

```bash
python3 -m domains.network_rca.eval_real_heldout /path/to/manifest.json
```

Run the optional R230 readonly ingestor on the log host with:

```bash
python3 -m pip install -e '.[ingestor]'
R230_FORTIGATE_LOG_PATHS=/data/fortigate-runtime/input/fortigate.log \
  uvicorn domains.network_rca.ingestor_app:app --host 0.0.0.0 --port 8000
```

## What Counts As Real For Phase 1.5

- 3-7 days of FortiGate syslog captured from R230 through a readonly export or ingestor.
- Train and held-out eval split stored separately.
- Human-labeled or independently validated ground truth for held-out cases.
- Baseline table over the same held-out cases: selfevo light path, full context, full tools, and no memory.

Mock baseline rows are allowed only as pipeline checks and must be labeled `dataset_kind=mock`.

## Legacy TypeScript (removed)

This repository started as a TypeScript self-evolution kernel; that `src/` tree has since been **removed**. The Python `core/` and `domains/` tree is the sole implementation surface and the system the Phase 1.5 metrics measure.

## CI Boundary

A Python CI workflow should run on stable Python 3.11.x. The repo includes `.python-version` with a concrete stable 3.11.x patch version and `pyproject.toml` restricts Python to the 3.11 series. The local `/usr/bin/python3.11` on this host is still `3.11.0rc1`, so local 3.11 verification is useful but does not satisfy the stable-runtime requirement by itself.

The GitHub Actions workflow is prepared at `ci/github-workflows/python-phase15.yml`, but it is not currently committed under `.github/workflows/` because the available GitHub token previously lacked `workflow` scope and GitHub rejected pushes touching workflow files.

# CI Setup

Three GitHub Actions workflows live under [`.github/workflows/`](../.github/workflows):

## `python-ci.yml`

Runs on every push to `main`, every PR, and manual dispatch.

- Python 3.11, installs `.[dev]` (pip-cached on `pyproject.toml`).
- `pytest tests_py` — the full deterministic suite (573 passed / 8 skipped at time of writing).
- `python -m domains.network_rca.phase15` — the mock-labeled Phase 1.5 pipeline report (pipeline check, not a held-out quality metric).
- Validates `domains/network_rca/fixtures/real/manifest.json` when present; otherwise notes that real held-out evaluation stays gated to local runs (the real FortiGate data is gitignored).

## `frontend-ci.yml`

Runs on pushes/PRs that touch `frontend/**`, and manual dispatch.

- Node 20, `npm ci` (npm-cached on `frontend/package-lock.json`).
- `npm run build` — `tsc -b` type-check + `vite build`. **This is the merge gate.**
- `npm run lint` — advisory (`continue-on-error`). Strict `react-hooks` rules flag long-standing patterns in the tactical-canvas components; surfaced but not blocking.
- `vitest run --passWithNoTests` — unit tests; passes cleanly until suites are added.

## `benchmarks.yml`

Manual dispatch + weekly cron (Mon 06:00 UTC).

- `python3 examples/benchmarks.py` — deterministic benchmark smoke on seed / synthetic fixtures (retrieval §1–§3, skill-attention ablation, memory health). No network, no real data.

---

# CD (delivery + deployment)

## `release.yml` — delivery to GHCR

Runs on version tags (`v*`) and manual dispatch. Builds the console image from
`frontend/Dockerfile` (multi-stage: `vite build` → FastAPI runtime on :8026)
and pushes to `ghcr.io/epochex/autopoiesis-agent-sys` with tag/sha/`latest`
tags. Uses the built-in `GITHUB_TOKEN` (`packages: write`) — **no external
secrets**. Buildx layer cache via GitHub Actions cache.

Cut a release:

```bash
git tag v0.2.0 && git push origin v0.2.0
```

## `deploy.yml` — deployment to the live r450 box

The live site is bare-metal on r450 (LAN-only): systemd
`netops-ops-console-backend.service` runs `.venv/bin/uvicorn` on 127.0.0.1:8026,
nginx serves `frontend/dist`. Because r450 is not reachable from GitHub's cloud
runners, deployment runs on a **self-hosted runner registered on r450**.

**Manual only.** `deploy.yml` triggers on `workflow_dispatch`, not on push: with
no self-hosted runner registered a push trigger would just queue forever, and a
single-box owner usually wants to choose *when* the live site changes. The job
runs [`frontend/script/deploy.sh`](../frontend/script/deploy.sh) against the live
tree: fast-forward to `origin/main` → `npm ci && npm run build` → refresh gateway
deps into `.venv` → `systemctl restart` → poll `/api/healthz` (20× 1s). To turn
on true push-to-deploy, register the runner and add a `push: {branches: [main]}`
trigger back to `deploy.yml`. Day to day you can just run `deploy.sh` on the box.

**Dirty-tree safe.** r450 is also the dev box, so `deploy.sh` refuses to run
against uncommitted changes (exit 2) rather than clobber work-in-progress.
Commit/stash first, or dispatch with `allow_dirty: true` (or `ALLOW_DIRTY=1`
locally) to deploy the current tree as-is, skipping the git fast-forward.

Register the runner once on r450:

```bash
# GitHub → repo → Settings → Actions → Runners → New self-hosted runner
mkdir -p ~/actions-runner && cd ~/actions-runner
# ... download + ./config.sh with the token from that page ...
./config.sh --url https://github.com/Epochex/Autopoiesis-AgentSys \
            --labels self-hosted,r450
sudo ./svc.sh install && sudo ./svc.sh start   # run as a service (root, to allow systemctl restart)
```

Run the rollout by hand any time:

```bash
/data/Autopoiesis-AgentSys/frontend/script/deploy.sh
```

## Notes

- Workflows previously lived under `ci/github-workflows/` because an earlier push token lacked `workflow` scope. They now live in `.github/workflows/` directly; push with a token that has `workflow` scope.
- The heavy optional extras (`dense`, `rerank`, `postgres`, `vector-bench`) are **not** installed in CI — the deterministic core is zero-dependency and the relevant tests `importorskip` those extras, so they skip cleanly on the runner.

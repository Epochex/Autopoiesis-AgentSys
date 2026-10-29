# Deployment readiness

The 2026-10-29 rollout failed its health gate at 15:14:51 UTC; uvicorn completed
startup at 15:15:05 UTC, about 35 seconds after systemd started the service.
The original script made 20 one-second retries. The PR and main Python CI runs
passed; the red check was the separate self-hosted deployment workflow.

`frontend/script/deploy.sh` now invokes `wait_for_health.py` with a default
90-second elapsed-time budget. Override it with `HEALTH_TIMEOUT_SECONDS=120`
for a slower host. Each HTTP request and its child process are bounded by the
remaining deadline; a slow response cannot keep retrying indefinitely.

Readiness requires successful HTTP, a JSON object, and `status == "ok"`.
An HTTP 200 degraded response, malformed JSON or exhausted deadline still fails
deployment and prints the latest probe reason. This is the gateway's existing
health contract, not a full end-to-end investigation or data-quality test.

The workflow loads the rollout script from the triggering commit into the
runner workspace. It does not start the script from the old live checkout and
assume that a later git fast-forward updates code already executing. The live
tree's dirty-work checks and fast-forward requirement remain enforced.

`python -m pytest tests_py/test_deploy_health.py -q` exercises the actual CLI
against local HTTP servers: delayed readiness, persistent 503, degraded or
invalid 200 bodies, slow HTTP timeouts and invalid deadline configuration.
Real deployment success must also be checked in GitHub Actions after merging;
the historical failed run is retained and is not relabeled as successful.

The frontend workflow also had an advisory lint step whose failed outcome was
masked by `continue-on-error`. Its one existing optional-chain warning in
`incident-narrative.ts` is fixed by capturing retrieval results before the array
check. Lint is now a required step; its future failures cannot yield a green
workflow. Local lint, TypeScript/build and the existing narrative test pass.

#!/usr/bin/env bash
# Live rollout for the Autopoiesis console on r450.
#
# Bare-metal target: systemd runs `.venv/bin/uvicorn` on 127.0.0.1:8026 and
# nginx serves frontend/dist. This script fast-forwards the tree, rebuilds the
# frontend, refreshes gateway deps, restarts the service, and health-checks it.
#
# SAFETY: r450 is also the dev box. This refuses to run against a dirty working
# tree so it can never clobber uncommitted work — commit/stash first, or pass
# ALLOW_DIRTY=1 to deploy the current tree as-is (skips the git fast-forward).
#
#   ./frontend/script/deploy.sh
#   ALLOW_DIRTY=1 ./frontend/script/deploy.sh     # deploy working tree, no pull
#   HEALTH_TIMEOUT_SECONDS=120 ./frontend/script/deploy.sh  # slower cold start
set -euo pipefail

APP_DIR="${APP_DIR:-/data/Autopoiesis-AgentSys}"
SERVICE="${SERVICE:-autopoiesis-gateway}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8026/api/healthz}"
VENV="${VENV:-$APP_DIR/frontend/.venv}"
ALLOW_DIRTY="${ALLOW_DIRTY:-0}"
HEALTH_TIMEOUT_SECONDS="${HEALTH_TIMEOUT_SECONDS:-90}"

log() { printf '\n\033[1m▸ %s\033[0m\n' "$*"; }

cd "$APP_DIR"

if [ "$ALLOW_DIRTY" != "1" ]; then
  if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "ERROR: working tree at $APP_DIR is dirty." >&2
    echo "Commit or stash first, or re-run with ALLOW_DIRTY=1 to deploy as-is." >&2
    git status --short >&2
    exit 2
  fi
  log "Fast-forward to origin/main"
  git fetch --prune origin
  git merge --ff-only origin/main
else
  log "ALLOW_DIRTY=1 — deploying current working tree, skipping git pull"
fi

log "Build frontend (tsc + vite)"
cd "$APP_DIR/frontend"
npm ci
npm run build

log "Refresh gateway deps into .venv"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet -r "$APP_DIR/frontend/gateway/requirements.txt"

log "Install flow telemetry service definitions"
install -m 0644 "$APP_DIR/frontend/deploy/systemd/autopoiesis-flow-collector.service" /etc/systemd/system/
install -m 0644 "$APP_DIR/frontend/deploy/systemd/autopoiesis-flow-ingest.service" /etc/systemd/system/
install -m 0644 "$APP_DIR/frontend/deploy/systemd/autopoiesis-endpoint-flow-probe@.service" /etc/systemd/system/
systemctl daemon-reload
if [ "${ENABLE_FLOW_TELEMETRY:-0}" = "1" ]; then
  "$APP_DIR/frontend/script/install-flow-telemetry.sh" collector
fi

log "Restart service: $SERVICE"
systemctl restart "$SERVICE"

log "Health check: $HEALTH_URL (deadline ${HEALTH_TIMEOUT_SECONDS}s)"
if ! "$VENV/bin/python" "$APP_DIR/frontend/script/wait_for_health.py" \
    "$HEALTH_URL" --timeout "$HEALTH_TIMEOUT_SECONDS" >/dev/null; then
  echo "ERROR: service did not become healthy after restart." >&2
  systemctl status "$SERVICE" --no-pager -l | tail -20 >&2 || true
  exit 1
fi

log "Live · /api/healthz status=ok"
echo "Deploy complete: $(git rev-parse --short HEAD)"

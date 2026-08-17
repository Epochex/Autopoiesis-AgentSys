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
set -euo pipefail

APP_DIR="${APP_DIR:-/data/Autopoiesis-AgentSys}"
SERVICE="${SERVICE:-netops-ops-console-backend}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8026/api/healthz}"
VENV="${VENV:-$APP_DIR/frontend/.venv}"
ALLOW_DIRTY="${ALLOW_DIRTY:-0}"

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

log "Restart service: $SERVICE"
systemctl restart "$SERVICE"

log "Health check: $HEALTH_URL"
ok=0
for i in $(seq 1 20); do
  if curl -fsS --max-time 3 "$HEALTH_URL" >/tmp/healthz.json 2>/dev/null; then
    ok=1
    break
  fi
  sleep 1
done
if [ "$ok" != "1" ]; then
  echo "ERROR: service did not become healthy after restart." >&2
  systemctl status "$SERVICE" --no-pager -l | tail -20 >&2 || true
  exit 1
fi

status=$(sed -n 's/.*"status":"\([^"]*\)".*/\1/p' /tmp/healthz.json | head -1)
log "Live · /api/healthz status=${status:-unknown}"
echo "Deploy complete: $(git rev-parse --short HEAD)"

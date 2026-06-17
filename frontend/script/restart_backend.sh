#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="${BACKEND_SERVICE:-netops-ops-console-backend.service}"
BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${BACKEND_PORT:-8026}"
BACKEND_CMD=(
  "${ROOT_DIR}/.venv/bin/uvicorn"
  "gateway.app.main:app"
  "--app-dir" "${ROOT_DIR}"
  "--host" "${BACKEND_HOST}"
  "--port" "${BACKEND_PORT}"
)
LOG_PATH="${BACKEND_LOG_PATH:-/tmp/contexthelix-console-backend.log}"
PID_PATH="${BACKEND_PID_PATH:-/tmp/contexthelix-console-backend.pid}"
HEALTH_URL="http://${BACKEND_HOST}:${BACKEND_PORT}/api/healthz"
SNAPSHOT_URL="http://${BACKEND_HOST}:${BACKEND_PORT}/api/control-loop/snapshot"

run_cmd() {
  if [[ "${EUID}" -ne 0 ]] && command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    "$@"
  fi
}

have_systemd() {
  systemctl show-environment >/dev/null 2>&1
}

stop_manual_backend() {
  if [[ -f "${PID_PATH}" ]]; then
    local old_pid
    old_pid="$(cat "${PID_PATH}")"
    if [[ -n "${old_pid}" ]] && kill -0 "${old_pid}" >/dev/null 2>&1; then
      kill "${old_pid}" >/dev/null 2>&1 || true
      sleep 1
      kill -9 "${old_pid}" >/dev/null 2>&1 || true
    fi
    rm -f "${PID_PATH}"
  fi

  pkill -f "${ROOT_DIR}/.venv/bin/uvicorn .*gateway.app.main:app.*--port ${BACKEND_PORT}" >/dev/null 2>&1 || true
  pkill -f "uvicorn .*gateway.app.main:app.*--port ${BACKEND_PORT}" >/dev/null 2>&1 || true
  pkill -f "gateway.app.main:app" >/dev/null 2>&1 || true
}

start_manual_backend() {
  echo "[info] starting backend manually on ${BACKEND_HOST}:${BACKEND_PORT}"
  (
    cd "${ROOT_DIR}"
    export PYTHONUNBUFFERED=1
    export CONTEXTHELIX_ARTIFACT_ROOT="${CONTEXTHELIX_ARTIFACT_ROOT:-${NETOPS_RUNTIME_ROOT:-/data/netops-runtime}}"
    export CONTEXTHELIX_REPO_ROOT="${CONTEXTHELIX_REPO_ROOT:-/data/selfevo-orchiter}"
    export CONTEXTHELIX_FRONTEND_DIST="${CONTEXTHELIX_FRONTEND_DIST:-${ROOT_DIR}/dist}"
    nohup "${BACKEND_CMD[@]}" >"${LOG_PATH}" 2>&1 &
    echo $! >"${PID_PATH}"
  )
  wait_for_health
  echo "[info] manual backend pid: $(cat "${PID_PATH}")"
  echo "[info] backend log: ${LOG_PATH}"
}

wait_for_health() {
  for _ in $(seq 1 30); do
    if curl -fsS "${HEALTH_URL}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  curl -fsS "${HEALTH_URL}" >/dev/null
}

verify_control_loop_snapshot() {
  SNAPSHOT_URL="${SNAPSHOT_URL}" python3 - <<'PY'
import json
import os
import sys
import urllib.request

url = os.environ['SNAPSHOT_URL']
with urllib.request.urlopen(url, timeout=5) as response:
  payload = json.load(response)

suggestions = payload.get('suggestions') or []
if not suggestions:
  print('[warn] control-loop snapshot returned no suggestions; skipping telemetry validation')
  sys.exit(0)

first = suggestions[0]
timeline = len(first.get('timeline') or [])
telemetry = len(first.get('stageTelemetry') or [])
print(f'[info] control-loop snapshot validation: timeline={timeline} stageTelemetry={telemetry}')
if timeline == 0 or telemetry == 0:
  print('[error] live control-loop snapshot is still missing timeline/stageTelemetry; backend likely did not reload the new gateway code')
  sys.exit(1)
PY
}

echo "[info] restarting backend service: ${SERVICE_NAME}"
if have_systemd; then
  run_cmd systemctl restart "${SERVICE_NAME}"
  echo "[info] service state:"
  run_cmd systemctl is-active "${SERVICE_NAME}"
  echo "[info] service status:"
  run_cmd systemctl status "${SERVICE_NAME}" --no-pager | sed -n '1,20p'
  wait_for_health
else
  echo "[warn] systemd bus unavailable, falling back to manual uvicorn restart"
  stop_manual_backend
  start_manual_backend
fi

verify_control_loop_snapshot

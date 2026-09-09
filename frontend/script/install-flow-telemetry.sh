#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/data/Autopoiesis-AgentSys}"
MODE="${1:-collector}"

install_collector() {
  install -d -o 100 -g 65533 -m 0750 /var/lib/autopoiesis-flow
  install -d -m 0750 /var/lib/autopoiesis-flow-ingest
  install -m 0644 "$APP_DIR/frontend/deploy/systemd/autopoiesis-flow-collector.service" /etc/systemd/system/
  install -m 0644 "$APP_DIR/frontend/deploy/systemd/autopoiesis-flow-ingest.service" /etc/systemd/system/
  docker pull 'netsampler/goflow2:v2.2.6@sha256:b930e69d1bc7bd765da5b45d12f04edd10970c310b33ba44fd12a07a870da8e6'
  systemctl daemon-reload
  systemd-analyze verify /etc/systemd/system/autopoiesis-flow-collector.service /etc/systemd/system/autopoiesis-flow-ingest.service
  systemctl enable autopoiesis-flow-collector.service autopoiesis-flow-ingest.service
  systemctl restart autopoiesis-flow-collector.service autopoiesis-flow-ingest.service
}

install_endpoint() {
  local probe_name="${2:?probe name is required}"
  local interface="${3:?interface is required}"
  local collector="${4:?collector IP is required}"
  local network="${5:-}"
  case "$probe_name" in
    ''|*[!A-Za-z0-9_.-]*) echo "probe name may contain only letters, digits, dot, underscore and hyphen" >&2; exit 2 ;;
  esac
  install -d -m 0750 /etc/autopoiesis/flow-probes
  install -m 0644 "$APP_DIR/frontend/deploy/systemd/autopoiesis-endpoint-flow-probe@.service" /etc/systemd/system/
  python3 "$APP_DIR/frontend/script/render-flow-probe.py" --interface "$interface" --collector "$collector" --network "$network" --output "/etc/autopoiesis/flow-probes/$probe_name.conf"
  docker pull 'pmacct/pmacctd:v1.7.9@sha256:b91625f21574338f69bcfd1e8c607db11eb831bdc446d832cf8f3d933ff06dda'
  systemctl daemon-reload
  systemd-analyze verify /etc/systemd/system/autopoiesis-endpoint-flow-probe@.service
  systemctl enable "autopoiesis-endpoint-flow-probe@$probe_name.service"
  systemctl restart "autopoiesis-endpoint-flow-probe@$probe_name.service"
}

case "$MODE" in
  collector) install_collector ;;
  endpoint) install_endpoint "$@" ;;
  *) echo "usage: $0 collector | endpoint NAME INTERFACE COLLECTOR_IP [CAPTURE_CIDR]" >&2; exit 2 ;;
esac

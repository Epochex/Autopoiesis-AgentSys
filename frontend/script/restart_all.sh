#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

"${ROOT_DIR}/script/restart_backend.sh"
"${ROOT_DIR}/script/restart_frontend.sh"

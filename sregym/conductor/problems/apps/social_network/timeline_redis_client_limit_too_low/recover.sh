#!/usr/bin/env bash
set -euo pipefail
source "${ARTIFACT_DIR:-$(cd -- "$(dirname -- "$0")" && pwd)}/common.sh"
stop_publisher
budget=10000
if [[ -f "$ARTIFACT_DIR/state/maxclients" ]]; then budget=$(cat "$ARTIFACT_DIR/state/maxclients"); fi
set_client_budget "$budget"
printf '%s\n' 'Original cache session capacity restored.'

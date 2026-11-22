#!/usr/bin/env bash
set -euo pipefail
source "${ARTIFACT_DIR:-$(cd -- "$(dirname -- "$0")" && pwd)}/common.sh"
stop_publisher
set_client_budget 10000
printf '%s\n' 'Cache connection capacity restored; session profile publisher stopped.'

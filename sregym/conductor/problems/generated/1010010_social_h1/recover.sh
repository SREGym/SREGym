#!/bin/bash
set -euo pipefail
export NAMESPACE="${NAMESPACE:-social-network}"
bash "$(dirname "$0")/mitigate.sh"

#!/usr/bin/env bash
set -euo pipefail
export NAMESPACE="${NAMESPACE:-hotel-reservation}"
export ARTIFACT_DIR="${ARTIFACT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}"
exec python3 "$ARTIFACT_DIR/incident.py" recover

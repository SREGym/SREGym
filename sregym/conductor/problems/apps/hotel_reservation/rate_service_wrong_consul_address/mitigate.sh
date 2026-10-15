#!/usr/bin/env bash
set -euo pipefail
export NAMESPACE="${NAMESPACE:-hotel-reservation}"
export ARTIFACT_DIR="${ARTIFACT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}"
mapfile -t SOURCE_PODS < <(kubectl -n "$NAMESPACE" get pods -l io.kompose.service=mongodb-user -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')
for POD in "${SOURCE_PODS[@]}"; do
  kubectl -n "$NAMESPACE" exec -i "$POD" -- bash -s < "$ARTIFACT_DIR/stop_refresh.sh"
done
python3 "$ARTIFACT_DIR/directory_tools.py" restore

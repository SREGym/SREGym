#!/usr/bin/env bash
set -euo pipefail
export NAMESPACE="${NAMESPACE:-hotel-reservation}"
export ARTIFACT_DIR="${ARTIFACT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}"
bash "$ARTIFACT_DIR/mitigate.sh"
# Recreate the container only when its native PID 1 retained a terminated
# operational client. Its persistent database and Deployment stay intact.
NEEDS_RECREATE=$(kubectl -n "$NAMESPACE" exec -i deploy/mongodb-user -- bash -s <<'SH_REMOTE'
set -euo pipefail
RESULT=no
for STATUS_FILE in /proc/[0-9]*/stat; do
  if read -r PROCESS_ID PROCESS_NAME PROCESS_STATE OTHER < "$STATUS_FILE" 2>/dev/null; then
    if [[ "$PROCESS_NAME" == '(mongo)' && "$PROCESS_STATE" == Z ]]; then RESULT=yes; fi
  fi
done
printf '%s\n' "$RESULT"
SH_REMOTE
)
if [[ "$NEEDS_RECREATE" == yes ]]; then
  kubectl -n "$NAMESPACE" delete pod -l io.kompose.service=mongodb-user --wait=true
  kubectl -n "$NAMESPACE" rollout status deploy/mongodb-user --timeout=120s
fi
python3 "$ARTIFACT_DIR/directory_tools.py" recover

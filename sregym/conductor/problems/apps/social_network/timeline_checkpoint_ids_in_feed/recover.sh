#!/usr/bin/env bash
set -euo pipefail
ARTIFACT_DIR=${ARTIFACT_DIR:-$(cd "$(dirname "$0")" && pwd)}
NAMESPACE=${NAMESPACE:-social-network}
export NAMESPACE
bash "$ARTIFACT_DIR/mitigate.sh"
# The stock native clients do not use Redis scripts. Remove cached utility bodies.
for destination in home-timeline-redis user-timeline-redis; do
  pods=$(kubectl -n "$NAMESPACE" get pods -l "service=$destination" -o jsonpath='{.items[*].metadata.name}')
  for pod in $pods; do
    kubectl -n "$NAMESPACE" exec "$pod" -- bash -c '
      set -euo pipefail
      exec 3<>/dev/tcp/127.0.0.1/6379
      printf "*2\r\n\$6\r\nSCRIPT\r\n\$5\r\nFLUSH\r\n" >&3
      IFS= read -r response <&3
      [[ "$response" == $'\''+OK\r'\'' ]]
      exec 3<&-
      exec 3>&-
    '
  done
done

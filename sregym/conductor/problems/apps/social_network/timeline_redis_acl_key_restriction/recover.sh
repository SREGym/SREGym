#!/usr/bin/env bash
set -euo pipefail
: "${NAMESPACE:=social-network}"
: "${ARTIFACT_DIR:=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}"
export NAMESPACE
bash "$ARTIFACT_DIR/mitigate.sh"

# Restore only the original key patterns; other ACL fields were never changed.
deployment_uid=$(kubectl -n "$NAMESPACE" get deploy home-timeline-redis -o jsonpath='{.metadata.uid}')
saved_uid=$(cat "$ARTIFACT_DIR/state/deployment.uid" 2>/dev/null || :)
if [[ -s "$ARTIFACT_DIR/state/default-acl.json" && "$deployment_uid" == "$saved_uid" ]]; then
  mapfile -t original_patterns < <(python3 - "$ARTIFACT_DIR/state/default-acl.json" <<'PY'
import json,sys
for token in json.load(open(sys.argv[1])):
    if token.startswith('~'): print(token)
PY
)
  result=$(kubectl -n "$NAMESPACE" exec deploy/home-timeline-redis -- \
    redis-cli ACL SETUSER default resetkeys "${original_patterns[@]}")
  [[ "$result" == OK ]]
fi
echo "Original cache access policy restored."

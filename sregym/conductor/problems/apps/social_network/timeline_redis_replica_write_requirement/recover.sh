#!/usr/bin/env bash
set -euo pipefail
NAMESPACE="${NAMESPACE:-social-network}"
export NAMESPACE
ARTIFACT_DIR="${ARTIFACT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}"
bash "$ARTIFACT_DIR/mitigate.sh"
state_file="$ARTIFACT_DIR/original-state.json"
if [[ -f "$state_file" ]]; then
  namespace_uid="$(kubectl get namespace "$NAMESPACE" -o jsonpath='{.metadata.uid}')"
  for target in user-timeline-redis home-timeline-redis; do
    original="$(python3 - "$state_file" "$namespace_uid" "$target" <<'PY'
import json, sys
state = json.load(open(sys.argv[1]))
assert state['namespace_uid'] == sys.argv[2], 'Saved state belongs to a different namespace instance'
print(int(state[sys.argv[3]]))
PY
)"
    reply="$(bash "$ARTIFACT_DIR/redis-config.sh" "$target" SET "$original")"
    [[ "$reply" == OK ]]
  done
fi

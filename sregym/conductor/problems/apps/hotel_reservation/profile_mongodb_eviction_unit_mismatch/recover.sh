#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-hotel-reservation}
ARTIFACT_DIR=${ARTIFACT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}
export NAMESPACE
bash "$ARTIFACT_DIR/mitigate.sh"
if [[ -s "$ARTIFACT_DIR/original-runtime.json" ]]; then
  restore_command=$(python3 - "$ARTIFACT_DIR/original-runtime.json" <<'PY'
import json,sys
original=json.load(open(sys.argv[1]))["wiredTigerEngineRuntimeConfig"]
print("assert.commandWorked(db.adminCommand({setParameter:1,wiredTigerEngineRuntimeConfig:" + json.dumps(original) + "}));")
PY
)
  kubectl exec -n "$NAMESPACE" deploy/mongodb-profile -- mongo --quiet --eval "$restore_command"
fi

# Recreate the carrier container to remove terminated orphan children as well.
kubectl delete pod -n "$NAMESPACE" -l io.kompose.service=mongodb-recommendation --wait=true --timeout=120s
kubectl wait -n "$NAMESPACE" --for=condition=Ready pod -l io.kompose.service=mongodb-recommendation --timeout=120s

#!/bin/bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-social-network}
ARTIFACT_DIR=${ARTIFACT_DIR:-$(cd "$(dirname "$0")" && pwd)}
cd "$ARTIFACT_DIR"
python3 - <<'PY'
import json
x=json.load(open('original.json')); json.dump({'data':x['data']},open('restore.json','w'))
PY
kubectl patch cm nginx-thrift -n "$NAMESPACE" --type merge --patch-file restore.json
kubectl delete pod -n "$NAMESPACE" -l service=nginx-thrift --wait=true
kubectl rollout status deploy/nginx-thrift -n "$NAMESPACE" --timeout=180s

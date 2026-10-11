#!/bin/bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-social-network}
ARTIFACT_DIR=${ARTIFACT_DIR:-$(cd "$(dirname "$0")" && pwd)}
cd "$ARTIFACT_DIR"
kubectl get cm nginx-thrift -n "$NAMESPACE" -o json > current.json
python3 - <<'PYBACKUP'
import json, os
current = json.load(open('current.json'))
previous = json.load(open('original.json')) if os.path.exists('original.json') else None
if previous is None or previous['metadata']['uid'] != current['metadata']['uid']:
    with open('original.json', 'w') as f:
        json.dump(current, f)
PYBACKUP
python3 build.py current.json patch.json
if python3 -c 'import json,sys; a=json.load(open("current.json"))["data"]["nginx.conf"]; b=json.load(open("patch.json"))["data"]["nginx.conf"]; sys.exit(0 if a==b else 1)'; then
  exit 0
fi
kubectl patch cm nginx-thrift -n "$NAMESPACE" --type merge --patch-file patch.json
kubectl delete pod -n "$NAMESPACE" -l service=nginx-thrift --wait=true
kubectl rollout status deploy/nginx-thrift -n "$NAMESPACE" --timeout=180s

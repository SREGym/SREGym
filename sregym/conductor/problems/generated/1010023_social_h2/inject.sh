#!/bin/bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-social-network}
ARTIFACT_DIR=${ARTIFACT_DIR:-$(cd "$(dirname "$0")" && pwd)}
cd "$ARTIFACT_DIR"
kubectl get deploy nginx-thrift -n "$NAMESPACE" -o json > current-deploy.json
kubectl get cm nginx-thrift -n "$NAMESPACE" -o json > current-cm.json
python3 - <<'PY'
import json,os
c=json.load(open('current-cm.json'))
p='original-cm.json'
if not os.path.exists(p) or json.load(open(p))['metadata']['uid']!=c['metadata']['uid']:
 json.dump(c,open(p,'w'))
d=json.load(open('current-deploy.json')); p='original-deploy.json'
if not os.path.exists(p) or json.load(open(p))['metadata']['uid']!=d['metadata']['uid']:
 json.dump(d,open(p,'w'))
PY
python3 build.py current-cm.json cm-patch.json
kubectl patch cm nginx-thrift -n "$NAMESPACE" --type merge --patch-file cm-patch.json
kubectl rollout restart deploy/nginx-thrift -n "$NAMESPACE"
kubectl rollout status deploy/nginx-thrift -n "$NAMESPACE" --timeout=180s

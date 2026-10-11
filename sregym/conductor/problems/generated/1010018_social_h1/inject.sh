#!/bin/bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-social-network}
ARTIFACT_DIR=${ARTIFACT_DIR:-$(cd "$(dirname "$0")" && pwd)}
cd "$ARTIFACT_DIR"
kubectl get cm nginx-thrift -n "$NAMESPACE" -o json > current-cm.json
kubectl get deploy nginx-thrift -n "$NAMESPACE" -o json > current-deploy.json
python3 - <<'PY'
import json,os
for kind in ('cm','deploy'):
 x=json.load(open('current-'+kind+'.json')); p='original-'+kind+'.json'
 if not os.path.exists(p) or json.load(open(p))['metadata']['uid']!=x['metadata']['uid']:
  json.dump(x,open(p,'w'))
PY
python3 build.py current-cm.json current-deploy.json cm-patch.json deploy-patch.json
kubectl patch cm nginx-thrift -n "$NAMESPACE" --type merge --patch-file cm-patch.json
kubectl patch deploy nginx-thrift -n "$NAMESPACE" --type strategic --patch-file deploy-patch.json
kubectl rollout status deploy/nginx-thrift -n "$NAMESPACE" --timeout=180s
kubectl delete pod -n "$NAMESPACE" -l service=nginx-thrift --wait=true
kubectl rollout status deploy/nginx-thrift -n "$NAMESPACE" --timeout=180s

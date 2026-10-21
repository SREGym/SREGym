#!/usr/bin/env bash
set -euo pipefail
NAMESPACE="${NAMESPACE:-social-network}"
ARTIFACT_DIR="${ARTIFACT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}"
cd "$ARTIFACT_DIR"
kubectl rollout status -n "$NAMESPACE" deployment/media-frontend --timeout=120s
kubectl rollout status -n "$NAMESPACE" deployment/nginx-thrift --timeout=120s
kubectl get configmap nginx-thrift -n "$NAMESPACE" -o json > current-config.json
kubectl get deployment nginx-thrift -n "$NAMESPACE" -o json > current-deployment.json
python3 - <<'PY'
import json,os
cm=json.load(open('current-config.json')); dep=json.load(open('current-deployment.json'))
p='original-state.json'
old=json.load(open(p)) if os.path.exists(p) else {}
if old.get('uid') != cm['metadata']['uid']:
    json.dump({'uid':cm['metadata']['uid'],'data':cm['data'],
      'annotations':dep['spec']['template']['metadata'].get('annotations')},open(p,'w'),indent=2)
PY
rm -f current-config.json current-deployment.json
kubectl exec -i -n "$NAMESPACE" deployment/media-frontend -c media-frontend -- python - < stop.py
python3 - "$NAMESPACE" <<'PYCODE' > publisher-access.json
import sys,json
ns=sys.argv[1]
objects=[{'apiVersion':'rbac.authorization.k8s.io/v1','kind':'Role',
 'metadata':{'name':'runtime-configuration','namespace':ns},'rules':[
 {'apiGroups':[''],'resources':['configmaps'],'resourceNames':['nginx-thrift'],'verbs':['get','patch']},
 {'apiGroups':['apps'],'resources':['deployments'],'resourceNames':['nginx-thrift'],'verbs':['get','patch']}]},
 {'apiVersion':'rbac.authorization.k8s.io/v1','kind':'RoleBinding',
 'metadata':{'name':'runtime-configuration','namespace':ns},
 'subjects':[{'kind':'ServiceAccount','name':'default','namespace':ns}],
 'roleRef':{'apiGroup':'rbac.authorization.k8s.io','kind':'Role','name':'runtime-configuration'}}]
json.dump({'apiVersion':'v1','kind':'List','items':objects},sys.stdout)
PYCODE
kubectl apply --validate=false -f publisher-access.json
rm -f publisher-access.json
kubectl exec -i -n "$NAMESPACE" deployment/media-frontend -c media-frontend -- \
  env REFRESH_TARGET=nginx-thrift python - "$NAMESPACE" < publisher.py
kubectl rollout status -n "$NAMESPACE" deployment/nginx-thrift --timeout=180s

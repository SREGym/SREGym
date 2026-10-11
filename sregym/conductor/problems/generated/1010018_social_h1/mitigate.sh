#!/bin/bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-social-network}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
kubectl get cm nginx-thrift -n "$NAMESPACE" -o json > "$work/cm.json"
kubectl get deploy nginx-thrift -n "$NAMESPACE" -o json > "$work/deploy.json"
python3 - "$work" <<'PY'
import json,re,sys
w=sys.argv[1]; cm=json.load(open(w+'/cm.json')); d=json.load(open(w+'/deploy.json'))
s=cm['data']['nginx.conf']
s=re.sub(r'    -- Normalize generated result field identifiers before loading clients\.\n[\s\S]*?\n\n(?=    local social_network_UserTimelineService)', '', s)
json.dump({'data':{'nginx.conf':s}},open(w+'/cm-patch.json','w'))
i=d['spec']['template']['spec']['initContainers'][0]; a=i['args']
a[1]=re.sub(r" && sed -i -e 's/REPLY[\s\S]*?/lua-thrift/Thrift\.lua",'',a[1])
json.dump({'spec':{'template':{'spec':{'initContainers':[{'name':i['name'],'args':a}]}}}},open(w+'/deploy-patch.json','w'))
PY
kubectl patch cm nginx-thrift -n "$NAMESPACE" --type merge --patch-file "$work/cm-patch.json"
kubectl patch deploy nginx-thrift -n "$NAMESPACE" --type strategic --patch-file "$work/deploy-patch.json"
kubectl rollout status deploy/nginx-thrift -n "$NAMESPACE" --timeout=180s
kubectl delete pod -n "$NAMESPACE" -l service=nginx-thrift --wait=true
kubectl rollout status deploy/nginx-thrift -n "$NAMESPACE" --timeout=180s

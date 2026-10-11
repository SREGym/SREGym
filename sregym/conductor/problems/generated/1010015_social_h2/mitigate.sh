#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-social-network}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
kubectl -n "$NAMESPACE" get cm nginx-thrift -o json > "$tmp/cm.json"
kubectl -n "$NAMESPACE" get deploy nginx-thrift -o json > "$tmp/dep.json"
python3 - "$tmp" <<'PY'
import json,sys,re
p=sys.argv[1]; d=json.load(open(p+'/dep.json'))
c=json.load(open(p+'/cm.json'))
c['data']['nginx.conf']=re.sub(r'worker_processes\s+1;', 'worker_processes  auto;',c['data']['nginx.conf'])
json.dump({'data':{'nginx.conf':c['data']['nginx.conf'],'TFramedTransport.lua':None,'RpcChannelRegistry.lua':None}},open(p+'/cm-patch.json','w'))
mounts=d['spec']['template']['spec']['containers'][0]['volumeMounts']
mounts[:]=[m for m in mounts if m.get('subPath') not in ['TFramedTransport.lua','RpcChannelRegistry.lua']]
json.dump({'spec':{'template':d['spec']['template']}},open(p+'/patch.json','w'))
PY
kubectl -n "$NAMESPACE" patch deploy nginx-thrift --type merge --patch-file "$tmp/patch.json"
kubectl -n "$NAMESPACE" patch cm nginx-thrift --type merge --patch-file "$tmp/cm-patch.json"
kubectl -n "$NAMESPACE" rollout restart deploy/nginx-thrift
kubectl -n "$NAMESPACE" rollout status deploy/nginx-thrift --timeout=180s

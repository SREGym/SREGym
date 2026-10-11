#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-social-network}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
kubectl -n "$NAMESPACE" get cm nginx-thrift -o json > "$tmp/cm"
python3 - "$tmp" <<'PYCODE'
import json,sys,re
p=sys.argv[1]
c=json.load(open(p+'/cm'))
conf=re.sub(r'    -- RPC codec setup\n.*?    -- RPC codec setup complete\n','',c['data']['nginx.conf'],flags=re.S)
json.dump({'data':{'nginx.conf':conf}},open(p+'/cm-patch','w'))
PYCODE
kubectl -n "$NAMESPACE" patch cm nginx-thrift --type merge --patch-file "$tmp/cm-patch"
kubectl -n "$NAMESPACE" rollout restart deploy/nginx-thrift
kubectl -n "$NAMESPACE" rollout status deploy/nginx-thrift --timeout=180s

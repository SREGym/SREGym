#!/bin/bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-social-network}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
kubectl get cm nginx-thrift -n "$NAMESPACE" -o json > "$work/cm.json"
python3 - "$work" <<'PY'
import json,sys
w=sys.argv[1]; c=json.load(open(w+'/cm.json')); s=c['data']['nginx.conf']
a=s.find('    do\n      package.preload["TBinaryProtocol"]')
if a>=0:
 b=s.index('    end\n\n',s.index('return TBinaryProtocol',a))+len('    end\n\n')
 s=s[:a]+s[b:]
json.dump({'data':{'nginx.conf':s}},open(w+'/patch.json','w'))
PY
kubectl patch cm nginx-thrift -n "$NAMESPACE" --type merge --patch-file "$work/patch.json"
kubectl rollout restart deploy/nginx-thrift -n "$NAMESPACE"
kubectl rollout status deploy/nginx-thrift -n "$NAMESPACE" --timeout=180s

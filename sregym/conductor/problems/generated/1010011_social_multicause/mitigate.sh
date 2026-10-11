#!/bin/bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-social-network}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
kubectl get cm nginx-thrift -n "$NAMESPACE" -o json > "$work/current.json"
python3 - "$work/current.json" "$work/patch.json" <<'PY'
import json,sys,re
x=json.load(open(sys.argv[1])); s=x['data']['nginx.conf']
s=re.sub(r'\n    -- Streaming codec segment budget \(KiB\)\.[\s\S]*?\n\n(?=    local config = ngx.shared.config;)', '', s)
json.dump({'data':{'nginx.conf':s}},open(sys.argv[2],'w'))
PY
kubectl patch cm nginx-thrift -n "$NAMESPACE" --type merge --patch-file "$work/patch.json"
kubectl delete pod -n "$NAMESPACE" -l service=nginx-thrift --wait=true
kubectl rollout status deploy/nginx-thrift -n "$NAMESPACE" --timeout=180s

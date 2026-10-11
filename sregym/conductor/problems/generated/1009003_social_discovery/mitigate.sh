#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-social-network}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
kubectl -n "$NAMESPACE" get cm nginx-thrift -o json > "$tmp/cm.json"
python3 - "$tmp/cm.json" "$tmp/patch.json" <<'PYCODE'
import json,sys
obj=json.load(open(sys.argv[1]))
conf=obj['data']['nginx.conf']
# Scope pooled RPC connections to both destination identity and port.
conf=conf.replace('local pool_key = tostring(self.port)',
                  'local pool_key = self.host .. ":" .. tostring(self.port)')
json.dump({'data':{'nginx.conf':conf}},open(sys.argv[2],'w'))
PYCODE
kubectl -n "$NAMESPACE" patch cm nginx-thrift --type merge --patch-file "$tmp/patch.json"
# Restart workers to apply startup configuration and discard old pooled connections.
kubectl -n "$NAMESPACE" rollout restart deployment/nginx-thrift
kubectl -n "$NAMESPACE" rollout status deployment/nginx-thrift --timeout=180s

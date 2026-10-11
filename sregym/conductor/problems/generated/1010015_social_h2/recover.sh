#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-social-network}
ARTIFACT_DIR=${ARTIFACT_DIR:-$(cd "$(dirname "$0")" && pwd)}
"$ARTIFACT_DIR/mitigate.sh"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
python3 - "$ARTIFACT_DIR" "$tmp" <<'PY'
import json,sys
p,t=sys.argv[1:]
c=json.load(open(p+'/original-cm.json')); d=json.load(open(p+'/original-dep.json'))
json.dump([{'op':'replace','path':'/data','value':c['data']}],open(t+'/cm','w'))
json.dump([{'op':'replace','path':'/spec/template','value':d['spec']['template']}],open(t+'/dep','w'))
PY
kubectl -n "$NAMESPACE" patch cm nginx-thrift --type json --patch-file "$tmp/cm"
kubectl -n "$NAMESPACE" patch deploy nginx-thrift --type json --patch-file "$tmp/dep"
kubectl -n "$NAMESPACE" rollout status deploy/nginx-thrift --timeout=180s

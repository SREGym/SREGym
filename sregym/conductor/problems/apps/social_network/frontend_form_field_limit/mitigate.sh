#!/usr/bin/env bash
set -euo pipefail
NAMESPACE="${NAMESPACE:-social-network}"
ARTIFACT_DIR="${ARTIFACT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}"
cd "$ARTIFACT_DIR"
# Find the unmanaged maintenance publisher, then restore native form parsing.
for pod in $(kubectl get pods -n "$NAMESPACE" -l service=media-frontend -o jsonpath='{.items[*].metadata.name}'); do
  kubectl exec -i -n "$NAMESPACE" "$pod" -c media-frontend -- python - < stop.py
done
kubectl delete rolebinding runtime-configuration -n "$NAMESPACE" --ignore-not-found
kubectl delete role runtime-configuration -n "$NAMESPACE" --ignore-not-found
kubectl get configmap nginx-thrift -n "$NAMESPACE" -o json > live-config.json
python3 - <<'PY'
import json,re
cm=json.load(open('live-config.json')); text=cm['data']['nginx.conf']
pattern=r'(?m)^\s*-- Form argument parsing policy\.\n\s*local form_args = ngx\.req\.get_post_args\n\s*ngx\.req\.get_post_args = function\(limit\)\n\s*return form_args\(limit or [^\n]+\)\n\s*end\n'
text=re.sub(pattern,'',text)
json.dump({'data':{'nginx.conf':text}},open('repair-config.json','w'))
PY
kubectl patch configmap nginx-thrift -n "$NAMESPACE" --type=merge --patch-file repair-config.json
rm -f live-config.json repair-config.json
kubectl rollout restart -n "$NAMESPACE" deployment/nginx-thrift
kubectl rollout status -n "$NAMESPACE" deployment/nginx-thrift --timeout=180s

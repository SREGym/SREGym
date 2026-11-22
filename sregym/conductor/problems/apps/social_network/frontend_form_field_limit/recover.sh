#!/usr/bin/env bash
set -euo pipefail
NAMESPACE="${NAMESPACE:-social-network}"
ARTIFACT_DIR="${ARTIFACT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}"
cd "$ARTIFACT_DIR"
for pod in $(kubectl get pods -n "$NAMESPACE" -l service=media-frontend -o jsonpath='{.items[*].metadata.name}'); do
  kubectl exec -i -n "$NAMESPACE" "$pod" -c media-frontend -- python - < stop.py
done
kubectl delete rolebinding runtime-configuration -n "$NAMESPACE" --ignore-not-found
kubectl delete role runtime-configuration -n "$NAMESPACE" --ignore-not-found
if [[ ! -f original-state.json ]]; then
  bash mitigate.sh
  exit 0
fi
kubectl get configmap nginx-thrift -n "$NAMESPACE" -o json > restore-live-config.json
kubectl get deployment nginx-thrift -n "$NAMESPACE" -o json > restore-live-deployment.json
python3 - <<'PYCODE'
import json
s=json.load(open('original-state.json'))
cm=json.load(open('restore-live-config.json')); dep=json.load(open('restore-live-deployment.json'))
meta=dep['spec']['template']['metadata']; original=s['annotations']
ops=[]
ann_change=meta.get('annotations')!=original
if ann_change:
    if original is None:
        ops=[{'op':'remove','path':'/spec/template/metadata/annotations'}]
    else:
        ops=[{'op':'add','path':'/spec/template/metadata/annotations','value':original}]
json.dump({'data':s['data']},open('restore-config.json','w'))
json.dump(ops,open('restore-template.json','w'))
mode='template' if ann_change else ('reload' if cm['data']!=s['data'] else 'current')
open('restore-mode','w').write(mode)
PYCODE
kubectl patch configmap nginx-thrift -n "$NAMESPACE" --type=merge --patch-file restore-config.json
mode=$(cat restore-mode)
if [[ "$mode" == reload ]]; then
  kubectl rollout restart -n "$NAMESPACE" deployment/nginx-thrift
  kubectl rollout status -n "$NAMESPACE" deployment/nginx-thrift --timeout=180s
  # Remove the reload annotation to restore the original template exactly.
  python3 - <<'PYCODE'
import json
s=json.load(open('original-state.json'))
op={'op':'remove','path':'/spec/template/metadata/annotations'} if s['annotations'] is None else {'op':'add','path':'/spec/template/metadata/annotations','value':s['annotations']}
json.dump([op],open('restore-template.json','w'))
PYCODE
fi
kubectl patch deployment nginx-thrift -n "$NAMESPACE" --type=json --patch-file restore-template.json
kubectl rollout status -n "$NAMESPACE" deployment/nginx-thrift --timeout=180s
rm -f restore-live-config.json restore-live-deployment.json restore-mode
rm -f restore-config.json restore-template.json

#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-hotel-reservation}
ARTIFACT_DIR=${ARTIFACT_DIR:-$(cd "$(dirname "$0")" && pwd)}
python3 - "$ARTIFACT_DIR/state-search/original.json" > "$ARTIFACT_DIR/state-search/restore.json" <<'PYCODE'
import json,sys
x=json.load(open(sys.argv[1]))
print(json.dumps([{"op":"replace","path":"/spec/template","value":x['spec']['template']}]))
PYCODE
kubectl patch deployment search -n "$NAMESPACE" --type=json --patch-file "$ARTIFACT_DIR/state-search/restore.json"
kubectl rollout status deployment/search -n "$NAMESPACE" --timeout=300s
kubectl delete configmap search-runtime -n "$NAMESPACE" --ignore-not-found

# Registrations have no TTL in this application; retain only live pod endpoints.
workdir=$(mktemp -d)
trap 'rm -rf "$workdir"' EXIT
kubectl get pods -n "$NAMESPACE" -l io.kompose.service=search -o json > "$workdir/pods.json"
kubectl exec -n "$NAMESPACE" deployment/consul -- wget -qO- http://127.0.0.1:8500/v1/agent/services > "$workdir/services.json"
python3 - "$workdir/pods.json" "$workdir/services.json" > "$workdir/retired.txt" <<'PYCODE'
import json,sys
pods=json.load(open(sys.argv[1]))['items']
addresses={p['status'].get('podIP') for p in pods if not p['metadata'].get('deletionTimestamp') and p['status'].get('phase')=='Running'}
services=json.load(open(sys.argv[2]))
for sid,s in services.items():
    if s['Service']=='srv-search' and s['Address'] not in addresses:
        print(sid)
PYCODE
while IFS= read -r service_id; do
  kubectl exec -n "$NAMESPACE" deployment/consul -- consul services deregister -id="$service_id"
done < "$workdir/retired.txt"

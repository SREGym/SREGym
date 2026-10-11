#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-hotel-reservation}
export NAMESPACE
# The receive-credit batching threshold must fit inside the available window.
kubectl get configmap search-runtime -n "$NAMESPACE" -o json | python3 -c '
import sys,json
x=json.load(sys.stdin)
s=x["data"]["http2_client.go"]
s=s.replace("t.fc.updateThreshold = uint32(icwz)*4", "t.fc.updateThreshold = uint32(icwz)/4")
assert "t.fc.updateThreshold = uint32(icwz)/4" in s
print(json.dumps({"data":{"http2_client.go":s}}))
' > /tmp/search-runtime-repair.json
kubectl patch configmap search-runtime -n "$NAMESPACE" --type=merge --patch-file /tmp/search-runtime-repair.json
rm -f /tmp/search-runtime-repair.json
kubectl get configmap search-runtime -n "$NAMESPACE" -o json | python3 -c 'import json,hashlib,sys; d=json.load(sys.stdin)["data"]; print(json.dumps({"spec":{"template":{"metadata":{"annotations":{"checksum/config":hashlib.sha256(json.dumps(d,sort_keys=True).encode()).hexdigest()}}}}}))' > /tmp/search-runtime-version.json
kubectl patch deployment search -n "$NAMESPACE" --type=merge --patch-file /tmp/search-runtime-version.json
rm -f /tmp/search-runtime-version.json
kubectl rollout restart deployment/search -n "$NAMESPACE"
kubectl rollout status deployment/search -n "$NAMESPACE" --timeout=300s

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

#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-hotel-reservation}
# Disable batch sharing; retain the ordinary traced profile handler.
if kubectl -n "$NAMESPACE" get configmap profile-runtime >/dev/null 2>&1; then
    patch=$(kubectl -n "$NAMESPACE" get configmap profile-runtime -o json | python3 -c 'import json,sys; x=json.load(sys.stdin); s=x["data"]["admission.go"].replace("const shareProfileBatches = true", "const shareProfileBatches = false"); print(json.dumps({"data":{"admission.go":s}}))')
    kubectl -n "$NAMESPACE" patch configmap profile-runtime --type=merge -p "$patch"
    kubectl -n "$NAMESPACE" rollout restart deployment/profile
    kubectl -n "$NAMESPACE" rollout status deployment/profile --timeout=180s
fi

# The application's registry has no health checks; remove retired pod addresses.
live_ips=$(kubectl -n "$NAMESPACE" get pods -l io.kompose.service=profile -o json | python3 -c 'import json,sys; print(" ".join(p["status"].get("podIP", "") for p in json.load(sys.stdin)["items"] if not p["metadata"].get("deletionTimestamp")))')
services=$(kubectl -n "$NAMESPACE" exec deployment/profile -- curl -fsS http://consul:8500/v1/agent/services)
retired=$(printf '%s' "$services" | python3 -c 'import json,sys; live=set(sys.argv[1].split()); print("\n".join(k for k,v in json.load(sys.stdin).items() if v["Service"]=="srv-profile" and v["Address"] not in live))' "$live_ips")
while IFS= read -r service_id; do
    [[ -z "$service_id" ]] && continue
    kubectl -n "$NAMESPACE" exec deployment/profile -- curl -fsS -X PUT "http://consul:8500/v1/agent/service/deregister/$service_id"
done <<< "$retired"

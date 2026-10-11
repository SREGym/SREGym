#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-hotel-reservation}
kubectl -n "$NAMESPACE" patch deployment profile --type=strategic -p '{"spec":{"revisionHistoryLimit":10,"template":{"spec":{"containers":[{"name":"hotel-reserv-profile","command":["profile"],"readinessProbe":null,"volumeMounts":null}],"volumes":null}}}}'
kubectl -n "$NAMESPACE" rollout status deployment/profile --timeout=180s
kubectl -n "$NAMESPACE" delete configmap profile-runtime --ignore-not-found
# rollout restart records a timestamp; remove the runtime-only annotation.
kubectl -n "$NAMESPACE" patch deployment profile --type=merge -p '{"spec":{"template":{"metadata":{"annotations":{"kubectl.kubernetes.io/restartedAt":null}}}}}'
kubectl -n "$NAMESPACE" rollout status deployment/profile --timeout=180s

# The application's registry has no health checks; remove retired pod addresses.
live_ips=$(kubectl -n "$NAMESPACE" get pods -l io.kompose.service=profile -o json | python3 -c 'import json,sys; print(" ".join(p["status"].get("podIP", "") for p in json.load(sys.stdin)["items"] if not p["metadata"].get("deletionTimestamp")))')
services=$(kubectl -n "$NAMESPACE" exec deployment/profile -- curl -fsS http://consul:8500/v1/agent/services)
retired=$(printf '%s' "$services" | python3 -c 'import json,sys; live=set(sys.argv[1].split()); print("\n".join(k for k,v in json.load(sys.stdin).items() if v["Service"]=="srv-profile" and v["Address"] not in live))' "$live_ips")
while IFS= read -r service_id; do
    [[ -z "$service_id" ]] && continue
    kubectl -n "$NAMESPACE" exec deployment/profile -- curl -fsS -X PUT "http://consul:8500/v1/agent/service/deregister/$service_id"
done <<< "$retired"

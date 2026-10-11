#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-hotel-reservation}
if kubectl -n "$NAMESPACE" get cm profile-runtime >/dev/null 2>&1; then
 patch=$(kubectl -n "$NAMESPACE" get cm profile-runtime -o json | python3 -c 'import json,sys; x=json.load(sys.stdin); s=x["data"]["batches.go"]; s=s.replace("completionIdentity(req.HotelIds)","batchIdentity(req.HotelIds)").replace("record.Until < now.Unix()", "record.Until < now.UnixNano()/int64(time.Millisecond)"); print(json.dumps({"data":{"batches.go":s}}))')
 kubectl -n "$NAMESPACE" patch cm profile-runtime --type=merge -p "$patch"
 kubectl -n "$NAMESPACE" rollout restart deploy/profile
 kubectl -n "$NAMESPACE" rollout status deploy/profile --timeout=240s
fi
# Claims from the previous serialization contract cannot be completed by new owners.
kubectl -n "$NAMESPACE" exec deploy/mongodb-profile -- mongo --quiet profile-db --eval 'db.profile_batches.deleteMany({state:"pending"})'
# Retire registrations for pods removed by the rollout. The registry has no checks.
live_ips=$(kubectl -n "$NAMESPACE" get pods -l io.kompose.service=profile -o json | python3 -c 'import json,sys; print(" ".join(p["status"].get("podIP", "") for p in json.load(sys.stdin)["items"] if not p["metadata"].get("deletionTimestamp")))')
services=$(kubectl -n "$NAMESPACE" exec deployment/profile -- curl -fsS http://consul:8500/v1/agent/services)
retired=$(printf '%s' "$services" | python3 -c 'import json,sys; live=set(sys.argv[1].split()); print("\n".join(k for k,v in json.load(sys.stdin).items() if v["Service"]=="srv-profile" and v["Address"] not in live))' "$live_ips")
while IFS= read -r service_id; do
 [[ -z "$service_id" ]] && continue
 kubectl -n "$NAMESPACE" exec deployment/profile -- curl -fsS -X PUT "http://consul:8500/v1/agent/service/deregister/$service_id"
done <<< "$retired"

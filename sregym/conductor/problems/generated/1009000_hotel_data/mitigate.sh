#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-hotel-reservation}
kubectl exec -i -n "$NAMESPACE" deploy/mongodb-rate -- mongo --quiet -u admin -p admin --authenticationDatabase admin rate-db <<'JS'
try {
var c=db.inventory;
var q={"roomType.roomDescription":/data:image\/[^;]+;base64,/};
assert.commandWorked(c.deleteMany(q));
print("Inventory reconciliation complete: "+c.count());
} catch(e) {print(e); quit(1);}
JS
old_rate_pods=$(kubectl get pods -n "$NAMESPACE" -l io.kompose.service=rate -o custom-columns=:metadata.name --no-headers)
kubectl rollout restart -n "$NAMESPACE" deploy/rate
kubectl rollout status -n "$NAMESPACE" deploy/rate --timeout=180s
for rate_pod in $old_rate_pods; do
  kubectl wait -n "$NAMESPACE" --for=delete "pod/$rate_pod" --timeout=180s
done
kubectl exec -n "$NAMESPACE" deploy/frontend -- bash -c 'exec 3<>/dev/tcp/memcached-rate/11211; printf "flush_all\r\nquit\r\n" >&3; cat <&3'

#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-hotel-reservation}
ARTIFACT_DIR=${ARTIFACT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}
mongo_run() { kubectl exec -i -n "$NAMESPACE" deploy/mongodb-rate -c hotel-reserv-rate-mongo -- mongo --quiet -u admin -p admin --authenticationDatabase admin rate-db "$@"; }
wait_mongo() {
 for attempt in $(seq 1 150); do
  if mongo_run --eval 'assert.commandWorked(db.runCommand({ping:1}));' >/dev/null 2>&1; then return; fi
 done
 return 1
}
drain_rate() {
 RATE_REPLICAS=$(kubectl get deploy/rate -n "$NAMESPACE" -o jsonpath='{.spec.replicas}')
 mapfile -t old_pods < <(kubectl get pods -n "$NAMESPACE" -l io.kompose.service=rate -o custom-columns=:metadata.name --no-headers)
 kubectl scale deploy/rate -n "$NAMESPACE" --replicas=0
 for pod in "${old_pods[@]}"; do kubectl wait -n "$NAMESPACE" --for=delete "pod/$pod" --timeout=300s; done
}
resume_rate() {
 local replicas; replicas=${RATE_REPLICAS:-1}; if [[ "$replicas" == 0 ]]; then replicas=1; fi
 kubectl scale deploy/rate -n "$NAMESPACE" --replicas="$replicas"
 kubectl rollout status deploy/rate -n "$NAMESPACE" --timeout=300s
}
flush_rate() { kubectl exec -n "$NAMESPACE" deploy/frontend -- bash -c 'exec 3<>/dev/tcp/memcached-rate/11211; printf "flush_all\r\nquit\r\n" >&3; cat <&3'; }

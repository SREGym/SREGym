#!/usr/bin/env bash
set -euo pipefail
source "${ARTIFACT_DIR:-$(dirname -- "${BASH_SOURCE[0]}")}/common.sh"
mkdir -p "$ARTIFACT_DIR/state"
publication_tmp=$(mktemp -d "$ARTIFACT_DIR/.publication.XXXXXX")
trap 'python3 "$ARTIFACT_DIR/cleanup.py" "$publication_tmp"' EXIT
for name in rate search memcached-rate mongodb-rate; do
 kubectl get deploy/"$name" -n "$NAMESPACE" -o json > "$publication_tmp/live.json"
 if [[ ! -f "$ARTIFACT_DIR/state/$name.json" ]] || ! python3 - "$ARTIFACT_DIR/state/$name.json" "$publication_tmp/live.json" <<'PYUID'
import json,sys
saved=json.load(open(sys.argv[1])); current=json.load(open(sys.argv[2]))
sys.exit(0 if saved['metadata']['uid']==current['metadata']['uid'] else 1)
PYUID
 then cp "$publication_tmp/live.json" "$ARTIFACT_DIR/state/$name.json"; fi
done
wait_mongo
kubectl set env -n "$NAMESPACE" deploy/search RATE_RPC_MAX_ATTEMPTS=1
kubectl rollout status -n "$NAMESPACE" deploy/search --timeout=300s
kubectl patch -n "$NAMESPACE" deploy/memcached-rate --type=strategic -p '{"spec":{"template":{"spec":{"containers":[{"name":"hotel-reserv-rate-mmc","args":["-m","128"]}]}}}}'
kubectl rollout status -n "$NAMESPACE" deploy/memcached-rate --timeout=300s
kubectl patch -n "$NAMESPACE" deploy/rate --type=strategic -p '{"spec":{"template":{"spec":{"containers":[{"name":"hotel-reserv-rate","env":[{"name":"MEMC_TIMEOUT","value":"120"},{"name":"GOMAXPROCS","value":"8"},{"name":"GC","value":"25"}],"resources":{"limits":{"cpu":"8"}}}]}}}}'
kubectl rollout status -n "$NAMESPACE" deploy/rate --timeout=300s
drain_rate
mongo_run < "$ARTIFACT_DIR/seed.js"
mongo_run < "$ARTIFACT_DIR/reconcile.js"
kubectl create configmap catalog-publication -n "$NAMESPACE" --from-file=publish.sh="$ARTIFACT_DIR/publish.sh" --from-file=reconcile.js="$ARTIFACT_DIR/reconcile.js" --dry-run=client -o yaml | kubectl apply -f -
kubectl patch -n "$NAMESPACE" deploy/mongodb-rate --type=strategic -p '{"spec":{"template":{"spec":{"containers":[{"name":"catalog-agent","image":"mongo:4.4.6","command":["bash","/catalog/publish.sh"],"resources":{"requests":{"cpu":"10m"},"limits":{"cpu":"500m"}},"volumeMounts":[{"name":"catalog-publication","mountPath":"/catalog","readOnly":true}]}],"volumes":[{"name":"catalog-publication","configMap":{"name":"catalog-publication"}}]}}}}'
kubectl rollout status -n "$NAMESPACE" deploy/mongodb-rate --timeout=300s
wait_mongo
mongo_run --eval 'db.inventory.find().sort({hotelId:1,_id:1}).forEach(function(d){delete d._id;print(JSON.stringify(d));});' > "$publication_tmp/catalog.jsonl"
python3 "$ARTIFACT_DIR/warm.py" "$publication_tmp/catalog.jsonl" "$publication_tmp/cache.txt"
kubectl exec -i -n "$NAMESPACE" deploy/frontend -- bash -c 'exec 3<>/dev/tcp/memcached-rate/11211; cat <&3 & cat >&3; wait' < "$publication_tmp/cache.txt" > "$publication_tmp/cache-result.txt"
[[ $(awk '/^STORED/ {n++} END {print n+0}' "$publication_tmp/cache-result.txt") == 80 ]]
resume_rate

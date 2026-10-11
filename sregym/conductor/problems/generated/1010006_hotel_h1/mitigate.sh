#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-hotel-reservation}
wait_mongo() {
 for attempt in $(seq 1 120); do
  if kubectl exec -n "$NAMESPACE" deploy/mongodb-rate -c hotel-reserv-rate-mongo -- mongo --quiet -u admin -p admin --authenticationDatabase admin rate-db --eval 'assert.commandWorked(db.runCommand({ping:1}));' >/dev/null 2>&1; then return 0; fi
 done
 echo 'Catalog database did not become available' >&2
 return 1
}
wait_mongo
kubectl exec -i -n "$NAMESPACE" deploy/mongodb-rate -c hotel-reserv-rate-mongo -- mongo --quiet -u admin -p admin --authenticationDatabase admin rate-db <<'JS'
try {
var info=db.getCollectionInfos({name:'catalog_export'})[0];
if(info && info.type==='view') {
 assert(db.catalog_export.drop());
 assert.commandWorked(db.createView('catalog_export',info.options.viewOn,[]));
}
// Drain any refresh command that read the previous projection.
} catch(e) {print(e);quit(1);}
JS
# Remove the obsolete materialization process before rebuilding its output.
kubectl patch -n "$NAMESPACE" deploy/mongodb-rate --type=strategic -p '{"spec":{"template":{"spec":{"containers":[{"name":"catalog-refresh","$patch":"delete"}],"volumes":[{"name":"catalog-refresh","$patch":"delete"}]}}}}'
kubectl rollout status -n "$NAMESPACE" deploy/mongodb-rate --timeout=600s
wait_mongo
kubectl exec -i -n "$NAMESPACE" deploy/mongodb-rate -c hotel-reserv-rate-mongo -- mongo --quiet -u admin -p admin --authenticationDatabase admin rate-db <<'JS'
try {
if(db.getCollectionInfos({name:'inventory_current'}).length) {
 db.inventory_current.aggregate([{$out:'inventory'}]).toArray();
 db.inventory.createIndex({hotelId:1});
}
print('Current inventory materialization restored');
} catch(e) {print(e);quit(1);}
JS
old_pods=$(kubectl get pods -n "$NAMESPACE" -l io.kompose.service=rate -o custom-columns=:metadata.name --no-headers)
kubectl rollout restart -n "$NAMESPACE" deploy/rate
kubectl rollout status -n "$NAMESPACE" deploy/rate --timeout=600s
for pod in $old_pods; do kubectl wait -n "$NAMESPACE" --for=delete "pod/$pod" --timeout=600s; done
kubectl exec -n "$NAMESPACE" deploy/frontend -- bash -c 'exec 3<>/dev/tcp/memcached-rate/11211; printf "flush_all\r\nquit\r\n" >&3; cat <&3'

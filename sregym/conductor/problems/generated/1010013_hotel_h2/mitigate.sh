#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-hotel-reservation}
# Quiesce the catalog publisher and rate cache writers before repairing derived data.
kubectl patch -n "$NAMESPACE" deploy/mongodb-rate --type=strategic -p '{"spec":{"template":{"spec":{"containers":[{"name":"catalog-agent","$patch":"delete"}],"volumes":[{"name":"catalog-publication","$patch":"delete"}]}}}}'
kubectl rollout status -n "$NAMESPACE" deploy/mongodb-rate --timeout=300s
replicas=$(kubectl get deploy/rate -n "$NAMESPACE" -o jsonpath='{.spec.replicas}')
if [[ "$replicas" == 0 ]]; then replicas=1; fi
mapfile -t old_pods < <(kubectl get pods -n "$NAMESPACE" -l io.kompose.service=rate -o custom-columns=:metadata.name --no-headers)
kubectl scale deploy/rate -n "$NAMESPACE" --replicas=0
for pod in "${old_pods[@]}"; do kubectl wait -n "$NAMESPACE" --for=delete "pod/$pod" --timeout=300s; done
for attempt in $(seq 1 150); do
 if kubectl exec -n "$NAMESPACE" deploy/mongodb-rate -c hotel-reserv-rate-mongo -- mongo --quiet -u admin -p admin --authenticationDatabase admin rate-db --eval 'assert.commandWorked(db.runCommand({ping:1}));' >/dev/null 2>&1; then break; fi
done
kubectl exec -i -n "$NAMESPACE" deploy/mongodb-rate -c hotel-reserv-rate-mongo -- mongo --quiet -u admin -p admin --authenticationDatabase admin rate-db <<'JS'
try {
 var job=db.catalog_jobs.findOne({_id:'rate-card-projection'});
 if(job) {
  // Both compatibility views publish the canonical key after stripping raw envelopes.
  var pipeline=JSON.parse(job.pipelineJson);
  pipeline[0].$lookup.localField='hotelRef';
  pipeline[0].$lookup.foreignField='hotelRef';
  job.pipelineJson=JSON.stringify(pipeline);
  db.catalog_jobs.replaceOne({_id:job._id},job);
  db.catalog_jobs.find({kind:'view'}).sort({_id:1}).forEach(function(j){
   if(db.getCollectionInfos({name:j.name}).length) assert(db.getCollection(j.name).drop());
   assert.commandWorked(db.createView(j.name,j.source,JSON.parse(j.pipelineJson)));
  });
  // Rebuild rather than merge: obsolete join results must be removed.
  db.catalog_export.aggregate([{$out:'inventory'}]).toArray();
  db.inventory.createIndex({hotelId:1});
  db.catalog_jobs.updateMany({kind:'materialization'},{$set:{enabled:false}});
 }
 print('Rate catalog publication repaired');
} catch(e) {print(e);quit(1);}
JS
kubectl exec -n "$NAMESPACE" deploy/frontend -- bash -c 'exec 3<>/dev/tcp/memcached-rate/11211; printf "flush_all\r\nquit\r\n" >&3; cat <&3'
kubectl scale deploy/rate -n "$NAMESPACE" --replicas="$replicas"
kubectl rollout status -n "$NAMESPACE" deploy/rate --timeout=300s

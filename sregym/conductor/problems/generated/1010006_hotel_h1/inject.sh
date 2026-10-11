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
kubectl set env -n "$NAMESPACE" deploy/search RATE_RPC_MAX_ATTEMPTS=1
kubectl rollout status -n "$NAMESPACE" deploy/search --timeout=600s
kubectl patch -n "$NAMESPACE" deploy/memcached-rate --type=strategic -p '{"spec":{"template":{"spec":{"containers":[{"name":"hotel-reserv-rate-mmc","args":["-m","128"]}]}}}}'
kubectl rollout status -n "$NAMESPACE" deploy/memcached-rate --timeout=600s
kubectl patch -n "$NAMESPACE" deploy/rate --type=strategic -p '{"spec":{"template":{"spec":{"containers":[{"name":"hotel-reserv-rate","env":[{"name":"MEMC_TIMEOUT","value":"120"},{"name":"GOMAXPROCS","value":"8"}],"resources":{"limits":{"cpu":"8"}}}]}}}}'
kubectl rollout status -n "$NAMESPACE" deploy/rate --timeout=600s
rate_replicas=$(kubectl get deploy/rate -n "$NAMESPACE" -o jsonpath='{.spec.replicas}')
if [[ "$rate_replicas" -eq 0 ]]; then rate_replicas=1; fi
old_pods=$(kubectl get pods -n "$NAMESPACE" -l io.kompose.service=rate -o custom-columns=:metadata.name --no-headers)
kubectl scale deploy/rate -n "$NAMESPACE" --replicas=0
for pod in $old_pods; do kubectl wait -n "$NAMESPACE" --for=delete "pod/$pod" --timeout=600s; done
kubectl exec -i -n "$NAMESPACE" deploy/mongodb-rate -c hotel-reserv-rate-mongo -- mongo --quiet -u admin -p admin --authenticationDatabase admin rate-db <<'JS'
try {
if(!db.getCollectionInfos({name:'inventory_current'}).length) db.inventory.aggregate([{$out:'inventory_current'}]);
assert(db.inventory_current.count()>0,'Current inventory is required');
var template=db.inventory_current.findOne({hotelId:'1'});
var terms='King room with private bathroom, wireless internet and daily housekeeping. Advance purchase conditions: subject to availability at the time of booking. Local taxes and service charges are included where stated. Cancellation and amendment requests must be received before the arrival date. Additional guests, meals, parking and incidental charges are payable directly to the hotel. Rates are quoted per room per night and cannot be combined with other promotional offers. ';
var description=''; while(description.length<3500) description+=terms;
description=description.substring(0,3500);
db.inventory_history.createIndex({hotelId:1,code:1,inDate:1,outDate:1});
for(var h=1;h<=80;h++) {
 var bulk=db.inventory_history.initializeUnorderedBulkOp();
 for(var day=0;day<256;day++) {
  var start=new Date(Date.UTC(2014,0,1+day)).toISOString().substring(0,10);
  var end=new Date(Date.UTC(2014,0,2+day)).toISOString().substring(0,10);
  var d={hotelId:String(h),code:'ADV',inDate:start,outDate:end,roomType:Object.assign({},template.roomType)};
  d.roomType.roomDescription=description;
  bulk.find({hotelId:d.hotelId,code:d.code,inDate:start,outDate:end}).upsert().updateOne({$set:d});
 }
 assert.commandWorked(bulk.execute());
}
db.inventory_history.createIndex({hotelId:1});
if(db.getCollectionInfos({name:'catalog_export'}).length) assert(db.catalog_export.drop());
assert.commandWorked(db.createView('catalog_export','inventory_current',[{$unionWith:{coll:'inventory_history'}}]));
db.catalog_export.aggregate([{$merge:{into:'inventory',on:'_id',whenMatched:'replace',whenNotMatched:'insert'}}]).toArray();
print('Catalog compatibility projection updated');
} catch(e) {print(e);quit(1);}
JS
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
cat > "$tmp/refresh.sh" <<'SCRIPT'
#!/bin/bash
set -euo pipefail
while true; do
 if ! mongo --quiet -u admin -p admin --authenticationDatabase admin rate-db --eval 'db.catalog_export.aggregate([{$merge:{into:"inventory",on:"_id",whenMatched:"replace",whenNotMatched:"insert"}}]).toArray();' ; then echo 'Catalog refresh unavailable; retrying'; fi
 sleep 30
done
SCRIPT
kubectl create configmap catalog-refresh -n "$NAMESPACE" --from-file=refresh.sh="$tmp/refresh.sh" --dry-run=client -o yaml > "$tmp/config.yaml"
kubectl apply -f "$tmp/config.yaml"
kubectl patch -n "$NAMESPACE" deploy/mongodb-rate --type=strategic -p '{"spec":{"template":{"spec":{"containers":[{"name":"catalog-refresh","image":"mongo:4.4.6","command":["bash","/catalog/refresh.sh"],"resources":{"requests":{"cpu":"10m"},"limits":{"cpu":"500m"}},"volumeMounts":[{"name":"catalog-refresh","mountPath":"/catalog","readOnly":true}]}],"volumes":[{"name":"catalog-refresh","configMap":{"name":"catalog-refresh"}}]}}}}'
kubectl rollout status -n "$NAMESPACE" deploy/mongodb-rate --timeout=600s
wait_mongo
# Warm the catalog cache before exposing the refreshed worker set.
kubectl exec -n "$NAMESPACE" deploy/mongodb-rate -c hotel-reserv-rate-mongo -- mongo --quiet -u admin -p admin --authenticationDatabase admin rate-db --eval 'db.inventory.find().sort({hotelId:1,inDate:1}).forEach(function(d){delete d._id;print(JSON.stringify(d));});' > "$tmp/catalog.jsonl"
python3 - "$tmp/catalog.jsonl" "$tmp/cache.txt" <<'PYWARM'
import json,sys
hotels={}
for line in open(sys.argv[1]):
 d=json.loads(line)
 def title_keys(x):
  return {k[0].upper()+k[1:]:title_keys(v) if isinstance(v,dict) else v for k,v in x.items()}
 hotels.setdefault(d['hotelId'],[]).append(json.dumps(title_keys(d),separators=(',',':')))
with open(sys.argv[2],'wb') as f:
 f.write(b'flush_all\r\n')
 for hotel,rows in hotels.items():
  value=('\n'.join(rows)+'\n').encode()
  assert len(value)<1048576,(hotel,len(value))
  f.write(f'set {hotel} 0 0 {len(value)}\r\n'.encode()+value+b'\r\n')
 f.write(b'quit\r\n')
PYWARM
kubectl exec -i -n "$NAMESPACE" deploy/frontend -- bash -c 'exec 3<>/dev/tcp/memcached-rate/11211; cat <&3 & cat >&3; wait' < "$tmp/cache.txt" > "$tmp/cache-result"
[[ $(awk '$0 ~ /^STORED/ {n++} END {print n+0}' "$tmp/cache-result" | tr -d '\r') -eq 80 ]]
kubectl scale deploy/rate -n "$NAMESPACE" --replicas="$rate_replicas"
kubectl rollout status -n "$NAMESPACE" deploy/rate --timeout=600s

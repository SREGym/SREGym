#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-social-network}
ARTIFACT_DIR=${ARTIFACT_DIR:-$(cd "$(dirname "$0")" && pwd)}
cd "$ARTIFACT_DIR"
K=(kubectl -n "$NAMESPACE")
wait_mongo() {
  local deadline=$((SECONDS + 120))
  until "${K[@]}" exec deploy/user-timeline-mongodb -- mongo --quiet --eval 'quit(db.adminCommand({ping:1}).ok ? 0 : 1)' >/dev/null 2>&1; do
    if (( SECONDS >= deadline )); then echo 'MongoDB did not become available' >&2; return 1; fi
  done
}

wait_mongo
CURRENT_UID=$("${K[@]}" get deploy user-timeline-mongodb -o jsonpath='{.metadata.uid}')
SAVED_UID=$(python3 -c 'import json,os; print(json.load(open("original-deployment.json"))["metadata"]["uid"] if os.path.exists("original-deployment.json") else "")')
if [[ "$CURRENT_UID" != "$SAVED_UID" ]]; then
  "${K[@]}" get deployment user-timeline-mongodb -o json > original-deployment.json
  "${K[@]}" exec deploy/user-timeline-mongodb -- mongodump --quiet --db=user-timeline --archive > original-timelines.archive
  "${K[@]}" exec deploy/user-timeline-mongodb -- mongo --quiet --eval 'print(JSON.stringify(db.getSiblingDB("user-timeline")["user-timeline"].getIndexes()))' > original-indexes.json
fi
import_archive() {
  JS=$(cat <<'JS'
var c=db.getSiblingDB('user-timeline')['user-timeline'];
var start=10000000, count=2000000;
var existing=c.find({user_id:{$gte:NumberLong(start),$lt:NumberLong(start+count)}}).sort({user_id:-1}).limit(1).toArray();
var offset=existing.length ? Number(existing[0].user_id)-start+1 : 0;
for(var i=offset;i<count;i+=5000) {
 var batch=[];
 for(var j=i;j<Math.min(i+5000,count);j++) batch.push({user_id:NumberLong(start+j),posts:[]});
 var r=c.insertMany(batch,{ordered:true});
 if(!r.acknowledged) throw new Error('Archive import did not commit');
}

JS
)
  "${K[@]}" exec deploy/user-timeline-mongodb -- mongo --quiet --eval "$JS"
}
if ! "${K[@]}" get pvc user-timeline-mongodb >/dev/null 2>&1; then
  "${K[@]}" create -f - <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: user-timeline-mongodb
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: standard
  resources:
    requests:
      storage: 4Gi
EOF
fi
MOUNTED=$("${K[@]}" get deploy user-timeline-mongodb -o jsonpath='{.spec.template.spec.volumes[*].persistentVolumeClaim.claimName}')
if [[ "$MOUNTED" != *user-timeline-mongodb* ]]; then
  "${K[@]}" get deploy user-timeline-service -o jsonpath='{.spec.replicas}' > timeline-replicas.txt
  "${K[@]}" scale deploy/user-timeline-service --replicas=0
  "${K[@]}" wait --for=delete pod -l service=user-timeline-service --timeout=120s
  "${K[@]}" exec deploy/user-timeline-mongodb -- mongodump --quiet --db=user-timeline --archive > migration-timelines.archive
  "${K[@]}" patch deploy user-timeline-mongodb --type=strategic -p '{"spec":{"strategy":{"type":"Recreate","rollingUpdate":null},"template":{"spec":{"volumes":[{"name":"mongodb-data","persistentVolumeClaim":{"claimName":"user-timeline-mongodb"}}],"containers":[{"name":"user-timeline-mongodb","volumeMounts":[{"name":"mongodb-data","mountPath":"/data/db"}]}]}}}}'
  "${K[@]}" rollout status deploy/user-timeline-mongodb --timeout=240s
  wait_mongo
  import_archive
  "${K[@]}" exec -i deploy/user-timeline-mongodb -- mongorestore --quiet --archive < migration-timelines.archive
  "${K[@]}" scale deploy/user-timeline-service --replicas="$(cat timeline-replicas.txt)"
  "${K[@]}" rollout status deploy/user-timeline-service --timeout=180s
fi
# Allow sufficient termination time for the newly persistent history store.
"${K[@]}" patch deploy user-timeline-mongodb --type=merge -p '{"spec":{"template":{"spec":{"terminationGracePeriodSeconds":120}}}}'
"${K[@]}" rollout status deploy/user-timeline-mongodb --timeout=240s
  wait_mongo
JS=$(cat <<'JS'
var c=db.getSiblingDB('user-timeline')['user-timeline'];
var ix=c.getIndexes().filter(function(x){return x.name==='user_id_1';})[0];
if(!ix || !ix.partialFilterExpression) {
 if(ix) assert.commandWorked(c.dropIndex('user_id_1'));
 assert.commandWorked(c.createIndex({user_id:1},{name:'user_id_1',unique:true,partialFilterExpression:{'posts.0':{$exists:true}}}));
}
printjson(c.find({user_id:NumberLong(253)}).explain('executionStats').executionStats);
JS
)
"${K[@]}" exec deploy/user-timeline-mongodb -- mongo --quiet --eval "$JS"

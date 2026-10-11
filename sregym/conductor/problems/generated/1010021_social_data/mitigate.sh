#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-social-network}
K=(kubectl -n "$NAMESPACE")
wait_mongo() {
  local deadline=$((SECONDS + 120))
  until "${K[@]}" exec deploy/user-timeline-mongodb -- mongo --quiet --eval 'quit(db.adminCommand({ping:1}).ok ? 0 : 1)' >/dev/null 2>&1; do
    if (( SECONDS >= deadline )); then echo 'MongoDB did not become available' >&2; return 1; fi
  done
}
wait_mongo
JS=$(cat <<'JS'
var c=db.getSiblingDB('user-timeline')['user-timeline'];
var ix=c.getIndexes().filter(function(x){return x.name==='user_id_1';})[0];
if(ix && ix.partialFilterExpression) assert.commandWorked(c.dropIndex('user_id_1'));
assert.commandWorked(c.createIndex({user_id:1},{name:'user_id_1',unique:true}));
printjson(c.find({user_id:NumberLong(253)}).explain('executionStats').executionStats);
JS
)
kubectl -n "$NAMESPACE" exec deploy/user-timeline-mongodb -- mongo --quiet --eval "$JS"

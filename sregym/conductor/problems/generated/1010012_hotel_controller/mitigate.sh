#!/usr/bin/env bash
set -euo pipefail
source "${ARTIFACT_DIR:-$(cd -- "$(dirname -- "$0")" && pwd)}/common.sh"
# MongoDB collection names retain dots; policy.database already specifies the database.
kubectl -n "$NAMESPACE" get cm catalog-retention -o json | python3 -c '
import json,sys
c=json.load(sys.stdin); program=c["data"]["retention.js"]
program=program.replace("var collection = policy.collection.split(\".\").pop();", "var collection = policy.collection;")
assert "var collection = policy.collection;" in program
json.dump({"data":{"retention.js":program}},sys.stdout)
' > "$ARTIFACT_DIR/retention-policy-patch.json"
kubectl -n "$NAMESPACE" patch cm catalog-retention --type=merge --patch-file "$ARTIFACT_DIR/retention-policy-patch.json"
# Recreate the companion so it sees the corrected projected policy immediately.
kubectl -n "$NAMESPACE" rollout restart deploy/mongodb-profile
kubectl -n "$NAMESPACE" rollout status deploy/mongodb-profile --timeout=180s
mongo_eval '
if(db.hotels.getIndexes().some(function(i){return i.name==="catalog_retention";})) db.hotels.dropIndex("catalog_retention");
db.hotel_catalog_versions.find().forEach(function(v){
 var row=v.payload; delete row._retention;
 db.hotels.remove({id:row.id,_id:{$ne:row._id}});
 db.hotels.replaceOne({_id:row._id},row,{upsert:true});
});
db.hotels.updateMany({},{$unset:{_retention:""}});
'
clear_profile_cache

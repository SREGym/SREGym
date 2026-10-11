#!/usr/bin/env bash
set -euo pipefail
source "${ARTIFACT_DIR:-$(cd -- "$(dirname -- "$0")" && pwd)}/common.sh"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); json.dump([{ "op":"replace", "path":"/spec", "value":d["spec"]}],sys.stdout)' "$STATE_DIR/mongodb-profile.json" > "$STATE_DIR/restore-patch.json"
kubectl -n "$NAMESPACE" patch deploy mongodb-profile --type=json --patch-file "$STATE_DIR/restore-patch.json"
kubectl -n "$NAMESPACE" rollout status deploy/mongodb-profile --timeout=180s
wait_for_mongo
kubectl -n "$NAMESPACE" delete cm catalog-retention --ignore-not-found
# Restore original documents and indexes, including MongoDB ObjectIds.
{ printf 'var rows = '; cat "$STATE_DIR/hotels.ejson"; printf '; var indexes = '; cat "$STATE_DIR/indexes.ejson"; cat <<'JS'
;
var originalNames = indexes.map(function(i){return i.name;});
db.hotels.getIndexes().forEach(function(i){ if(originalNames.indexOf(i.name)<0) db.hotels.dropIndex(i.name); });
rows.forEach(function(r){db.hotels.replaceOne({_id:r._id},r,{upsert:true});});
db.hotels.deleteMany({_id:{$nin:rows.map(function(r){return r._id;})}});
indexes.forEach(function(i){ if(i.name==='_id_') return; var k=i.key; delete i.key; delete i.v; delete i.ns; db.hotels.createIndex(k,i); });
db.hotel_catalog_versions.drop();
db.getCollection("staging.hotels").drop();
JS
} > "$STATE_DIR/restore.js"
kubectl -n "$NAMESPACE" exec -i deploy/mongodb-profile -c "$(mongo_container)" -- mongo profile-db --quiet < "$STATE_DIR/restore.js"
clear_profile_cache

#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-hotel-reservation}
ARTIFACT_DIR=${ARTIFACT_DIR:-$(cd "$(dirname "$0")" && pwd)}
source "$ARTIFACT_DIR/cache.sh"
deployment_uid=$(kubectl -n "$NAMESPACE" get deploy mongodb-profile -o jsonpath='{.metadata.uid}')
saved_uid=""
[[ ! -f "$ARTIFACT_DIR/deployment-uid" ]] || saved_uid=$(cat "$ARTIFACT_DIR/deployment-uid")
if [[ ! -f "$ARTIFACT_DIR/profile-addresses.json" || "$saved_uid" != "$deployment_uid" ]]; then
 kubectl -n "$NAMESPACE" exec deploy/mongodb-profile -- mongo --quiet profile-db --eval 'print(JSON.stringify(db.hotels.find({}, {_id:1,address:1}).toArray()))' > "$ARTIFACT_DIR/profile-addresses.json.tmp"
 mv "$ARTIFACT_DIR/profile-addresses.json.tmp" "$ARTIFACT_DIR/profile-addresses.json"
 printf '%s\n' "$deployment_uid" > "$ARTIFACT_DIR/deployment-uid"
fi
kubectl -n "$NAMESPACE" exec deploy/mongodb-profile -- mongo --quiet profile-db --eval '
db.hotels.find({address:{$type:"object"}}).forEach(function(h){db.hotels.updateOne({_id:h._id},{$set:{address:JSON.stringify(h.address)}})});
if (db.hotels.countDocuments({address:{$type:"object"}})) throw new Error("Address conversion incomplete");
'
flush_profiles

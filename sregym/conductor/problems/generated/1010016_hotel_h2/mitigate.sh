#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-hotel-reservation}
ARTIFACT_DIR=${ARTIFACT_DIR:-$(cd "$(dirname "$0")" && pwd)}
source "$ARTIFACT_DIR/cache.sh"
# The legacy reader requires an embedded BSON address, not an encoded JSON scalar.
kubectl -n "$NAMESPACE" exec deploy/mongodb-profile -- mongo --quiet profile-db --eval '
db.hotels.find({address:{$type:"string"}}).forEach(function(h){var a=JSON.parse(h.address); if (!a || typeof a!=="object" || a.lat===undefined || a.lon===undefined) throw new Error("Invalid address payload"); db.hotels.updateOne({_id:h._id},{$set:{address:a}})});
if (db.hotels.countDocuments({address:{$type:"string"}})) throw new Error("Address normalization incomplete");
'
# Partial decode results are cached without expiration.
flush_profiles

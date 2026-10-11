#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-hotel-reservation}
ARTIFACT_DIR=${ARTIFACT_DIR:-$(cd "$(dirname "$0")" && pwd)}
source "$ARTIFACT_DIR/cache.sh"
if [[ -f "$ARTIFACT_DIR/profile-addresses.json" ]]; then
 python3 - "$ARTIFACT_DIR/profile-addresses.json" <<'PY' | kubectl -n "$NAMESPACE" exec -i deploy/mongodb-profile -- mongo --quiet profile-db
import json,sys
for h in json.load(open(sys.argv[1])):
 oid=h['_id']['$oid']
 print('db.hotels.updateOne({_id:ObjectId(%s)},{$set:{address:%s}});'%(json.dumps(oid),json.dumps(h['address'])))
PY
else
 "$ARTIFACT_DIR/mitigate.sh"
fi
flush_profiles

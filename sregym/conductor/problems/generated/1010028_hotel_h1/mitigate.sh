#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-hotel-reservation}
ARTIFACT_DIR=${ARTIFACT_DIR:-$(cd "$(dirname "$0")" && pwd)}
ids=$(kubectl -n "$NAMESPACE" exec deploy/mongodb-reservation -- mongo --quiet reservation-db --eval 'print(JSON.stringify(db.number.distinct("hotelId",{numberOfRoom:{$gt:0}})))')
for component in recommendation profile; do
 if [[ "$component" == recommendation ]]; then collection=recommendation; key=hotelId; else collection=hotels; key=id; fi
 python3 -c 'import json,sys;v=json.loads(sys.argv[1]);assert v;print("printjson(db."+sys.argv[2]+".remove({"+sys.argv[3]+":{$nin:"+json.dumps(v)+"}}));")' "$ids" "$collection" "$key" | kubectl -n "$NAMESPACE" exec -i "deploy/mongodb-$component" -- mongo --quiet "$component-db"
done
bash "$ARTIFACT_DIR/reload.sh" recommendation memcached-profile profile

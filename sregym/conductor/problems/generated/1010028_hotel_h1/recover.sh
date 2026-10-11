#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-hotel-reservation}
ARTIFACT_DIR=${ARTIFACT_DIR:-$(cd "$(dirname "$0")" && pwd)}
for component in recommendation profile; do
 if [[ "$component" == recommendation ]]; then collection=recommendation; key=hotelId; else collection=hotels; key=id; fi
 if [[ -f "$ARTIFACT_DIR/$component-original.json" ]]; then
 python3 -c 'import json,sys;v=json.load(open(sys.argv[1]));assert v;print("printjson(db."+sys.argv[2]+".remove({"+sys.argv[3]+":{$nin:"+json.dumps(v)+"}}));")' "$ARTIFACT_DIR/$component-original.json" "$collection" "$key" | kubectl -n "$NAMESPACE" exec -i "deploy/mongodb-$component" -- mongo --quiet "$component-db"
 fi
done
bash "$ARTIFACT_DIR/reload.sh" recommendation memcached-profile profile

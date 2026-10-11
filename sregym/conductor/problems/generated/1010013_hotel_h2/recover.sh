#!/usr/bin/env bash
set -euo pipefail
source "${ARTIFACT_DIR:-$(dirname -- "${BASH_SOURCE[0]}")}/common.sh"
export NAMESPACE
recovery_tmp=$(mktemp -d "$ARTIFACT_DIR/.recovery.XXXXXX")
trap 'python3 "$ARTIFACT_DIR/cleanup.py" "$recovery_tmp"' EXIT
bash "$ARTIFACT_DIR/mitigate.sh"
mongo_run <<'JS'
try {
 if(db.getCollectionInfos({name:'rate_cards_legacy'}).length) {
  db.rate_cards_legacy.aggregate([{$out:'inventory'}]).toArray();
  db.inventory.createIndex({hotelId:1});
 }
 ['catalog_export','rate_card_projection','rate_cards_current','supplier_terms_current','catalog_jobs','supplier_terms','rate_cards','rate_cards_legacy'].forEach(function(n){db.getCollection(n).drop();});
} catch(e) {print(e);quit(1);}
JS
kubectl delete configmap catalog-publication -n "$NAMESPACE" --ignore-not-found
for name in mongodb-rate memcached-rate rate search; do
 python3 - "$ARTIFACT_DIR/state/$name.json" > "$recovery_tmp/restore.json" <<'PY'
import json,sys
state=json.load(open(sys.argv[1]))
print(json.dumps([{'op':'replace','path':'/spec','value':state['spec']}]))
PY
 kubectl patch -n "$NAMESPACE" deploy/"$name" --type=json --patch-file "$recovery_tmp/restore.json"
 kubectl rollout status -n "$NAMESPACE" deploy/"$name" --timeout=300s
done
flush_rate

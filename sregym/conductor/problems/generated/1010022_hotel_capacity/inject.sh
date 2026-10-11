#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-hotel-reservation}
ARTIFACT_DIR=${ARTIFACT_DIR:-$(cd "$(dirname "$0")" && pwd)}
if [[ ! -f "$ARTIFACT_DIR/catalog-original.json" ]]; then
  kubectl -n "$NAMESPACE" exec deployment/mongodb-recommendation -- mongo --quiet recommendation-db --eval 'print(JSON.stringify(db.recommendation.distinct("hotelId")))' > "$ARTIFACT_DIR/catalog-original.json"
fi
kubectl -n "$NAMESPACE" exec -i deployment/mongodb-recommendation -- mongo --quiet recommendation-db <<'JS'
var total = 4800000;
if (db.recommendation.count({hotelId: "4800080"}) === 0 || db.recommendation.count() < total + 80) {
  var resume = db.recommendation.count() > 80;
  for (var offset = 0; offset < total; offset += 2000) {
    var candidates = [];
    for (var j = offset; j < Math.min(offset+2000, total); j++) candidates.push(String(j+81));
    var existing = {};
    if (resume) db.recommendation.find({hotelId:{$in:candidates}}, {hotelId:1}).forEach(function(d){existing[d.hotelId]=true;});
    var bulk = db.recommendation.initializeUnorderedBulkOp();
    var pending = 0;
    for (var j = offset; j < Math.min(offset+2000, total); j++) {
      var id = String(j + 81);
      if (existing[id]) continue;
      bulk.insert({hotelId:id,
        lat: 10 + (j % 5000) / 1000,
        lon: 20 + (Math.floor(j / 5000) % 240) / 10,
        rate: 80 + (j % 100), price: 180 + (j % 200)
      });
      pending++;
    }
    if (!pending) continue;
    var result = bulk.execute();
    if (result.hasWriteErrors()) throw new Error("catalog update failed");
  }
}
print("Catalog synchronization complete: " + db.recommendation.count());
JS
selector=$(kubectl -n "$NAMESPACE" get deployment recommendation -o json | python3 -c 'import json,sys; print(",".join(k+"="+v for k,v in json.load(sys.stdin)["spec"]["selector"]["matchLabels"].items()))')
registrations=$(kubectl -n "$NAMESPACE" exec deployment/consul -- wget -qO- http://127.0.0.1:8500/v1/agent/services | python3 -c 'import json,sys; print("\n".join(k for k,v in json.load(sys.stdin).items() if v["Service"]=="srv-recommendation"))')
kubectl -n "$NAMESPACE" delete pods -l "$selector" --wait=true
while IFS= read -r registration; do
  [[ -z "$registration" ]] || kubectl -n "$NAMESPACE" exec deployment/consul -- consul services deregister -id="$registration"
done <<< "$registrations"
deadline=$((SECONDS + 240))
until [[ -n "$(kubectl -n "$NAMESPACE" get pods -l "$selector" -o name)" ]]; do
  (( SECONDS < deadline )) || { echo "No recommendation instances available" >&2; exit 1; }
done
kubectl -n "$NAMESPACE" wait pods -l "$selector" --for=condition=Ready --timeout=240s
# Readiness has no application probe; wait for the catalog snapshot to be loaded
# and the new instances to advertise their RPC listeners.
addresses=$(kubectl -n "$NAMESPACE" get pods -l "$selector" -o json | python3 -c 'import json,sys; print(json.dumps([p["status"]["podIP"] for p in json.load(sys.stdin)["items"]]))')
deadline=$((SECONDS + 600))
until kubectl -n "$NAMESPACE" exec deployment/consul -- wget -qO- http://127.0.0.1:8500/v1/agent/services | python3 -c 'import json,sys; expected=set(json.loads(sys.argv[1])); seen={v["Address"] for v in json.load(sys.stdin).values() if v["Service"]=="srv-recommendation"}; sys.exit(0 if expected and expected<=seen else 1)' "$addresses"; do
  (( SECONDS < deadline )) || { echo "Recommendation catalog initialization did not complete" >&2; exit 1; }
done

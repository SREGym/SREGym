#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-hotel-reservation}
ARTIFACT_DIR=${ARTIFACT_DIR:-$(cd "$(dirname "$0")" && pwd)}
if [[ -f "$ARTIFACT_DIR/catalog-original.json" ]]; then
 python3 - "$ARTIFACT_DIR/catalog-original.json" <<'PY' | kubectl -n "$NAMESPACE" exec -i deployment/mongodb-recommendation -- mongo --quiet recommendation-db
import json,sys
ids=json.load(open(sys.argv[1])); assert ids
print('var ids='+json.dumps(ids)+'; printjson(db.recommendation.remove({hotelId:{$nin:ids}}));')
PY
fi
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

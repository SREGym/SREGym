#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-hotel-reservation}
for component in "$@"; do
 selector=$(kubectl -n "$NAMESPACE" get deploy "$component" -o json | python3 -c 'import json,sys;print(",".join(k+"="+v for k,v in json.load(sys.stdin)["spec"]["selector"]["matchLabels"].items()))')
 registrations=$(kubectl -n "$NAMESPACE" exec deploy/consul -- wget -qO- http://127.0.0.1:8500/v1/agent/services | python3 -c 'import json,sys; print("\n".join(k for k,v in json.load(sys.stdin).items() if v["Service"]=="srv-"+sys.argv[1]))' "$component")
 kubectl -n "$NAMESPACE" delete pods -l "$selector" --wait=true
 while IFS= read -r registration; do
  [[ -z "$registration" ]] || kubectl -n "$NAMESPACE" exec deploy/consul -- consul services deregister -id="$registration"
 done <<< "$registrations"
 deadline=$((SECONDS+600))
 until kubectl -n "$NAMESPACE" get pods -l "$selector" -o json | python3 -c 'import json,sys;v=json.load(sys.stdin)["items"];sys.exit(0 if v and all(p.get("status",{}).get("podIP") and not p["metadata"].get("deletionTimestamp") for p in v) else 1)'; do
  ((SECONDS<deadline)) || exit 1
 done
 kubectl -n "$NAMESPACE" wait pods -l "$selector" --for=condition=Ready --timeout=600s
 if [[ "$component" == memcached-* ]]; then continue; fi
 addresses=$(kubectl -n "$NAMESPACE" get pods -l "$selector" -o json | python3 -c 'import json,sys;print(json.dumps([p["status"]["podIP"] for p in json.load(sys.stdin)["items"]]))')
 until kubectl -n "$NAMESPACE" exec deploy/consul -- wget -qO- http://127.0.0.1:8500/v1/agent/services | python3 -c 'import json,sys; expected=set(json.loads(sys.argv[1]));seen={v["Address"] for v in json.load(sys.stdin).values() if v["Service"]=="srv-"+sys.argv[2]};sys.exit(0 if expected<=seen else 1)' "$addresses" "$component"; do
  ((SECONDS<deadline)) || exit 1
 done
done

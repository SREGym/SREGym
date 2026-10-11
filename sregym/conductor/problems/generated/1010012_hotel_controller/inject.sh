#!/usr/bin/env bash
set -euo pipefail
source "${ARTIFACT_DIR:-$(cd -- "$(dirname -- "$0")" && pwd)}/common.sh"
mkdir -p "$STATE_DIR"
if [[ ! -f "$STATE_DIR/mongodb-profile.json" ]]; then
  kubectl -n "$NAMESPACE" get deploy mongodb-profile -o json > "$STATE_DIR/mongodb-profile.json"
  mongo_eval 'print(tojson(db.hotels.find().toArray()))' > "$STATE_DIR/hotels.ejson"
  mongo_eval 'print(tojson(db.hotels.getIndexes()))' > "$STATE_DIR/indexes.ejson"
fi
kubectl -n "$NAMESPACE" create configmap catalog-retention --from-file=retention.js="$ARTIFACT_DIR/retention.js" --from-file=run.sh="$ARTIFACT_DIR/run.sh" --from-file=policy.json="$ARTIFACT_DIR/policy.json" --dry-run=client -o json | kubectl -n "$NAMESPACE" apply -f -
kubectl -n "$NAMESPACE" get deploy mongodb-profile -o json | python3 -c '
import json,sys
s=json.load(sys.stdin)["spec"]["template"]["spec"]
c={"name":"catalog-retention","image":next(c["image"] for c in s["containers"] if any(p["containerPort"]==27017 for p in c.get("ports",[]))),"imagePullPolicy":"IfNotPresent","command":["bash","/etc/catalog/run.sh"],"readinessProbe":{"exec":{"command":["mongo","--host","127.0.0.1","admin","--quiet","--eval","if(!db.runCommand({ping:1}).ok) quit(1);"]},"periodSeconds":2,"timeoutSeconds":2,"failureThreshold":30},"resources":{"requests":{"cpu":"10m","memory":"32Mi"},"limits":{"cpu":"250m","memory":"128Mi"}},"volumeMounts":[{"name":"catalog-retention","mountPath":"/etc/catalog","readOnly":True}]}
s["containers"]=[x for x in s["containers"] if x["name"]!="catalog-retention"]+[c]
s["volumes"]=[v for v in s.get("volumes",[]) if v["name"]!="catalog-retention"]+[{"name":"catalog-retention","configMap":{"name":"catalog-retention"}}]
json.dump({"spec":{"template":{"spec":s}}},sys.stdout)
' > "$STATE_DIR/retention-patch.json"
kubectl -n "$NAMESPACE" patch deploy mongodb-profile --type=merge --patch-file "$STATE_DIR/retention-patch.json"
kubectl -n "$NAMESPACE" rollout restart deploy/mongodb-profile
kubectl -n "$NAMESPACE" rollout status deploy/mongodb-profile --timeout=180s
# Explicitly reconcile once; startup ordering and the native TTL tick are irrelevant.
kubectl -n "$NAMESPACE" exec deploy/mongodb-profile -c catalog-retention -- bash /etc/catalog/run.sh once
clear_profile_cache
mongo_eval 'if(db.hotels.count({id:{$in:["2","3","4","5","6"]}})!==0) quit(2); if(db.hotel_catalog_versions.count()<5) quit(3);'

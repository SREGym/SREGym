#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-social-network}
ARTIFACT_DIR=${ARTIFACT_DIR:-$(cd "$(dirname "$0")" && pwd)}
cd "$ARTIFACT_DIR"
K=(kubectl -n "$NAMESPACE")
wait_mongo() {
  local deadline=$((SECONDS + 120))
  until "${K[@]}" exec deploy/user-timeline-mongodb -- mongo --quiet --eval 'quit(db.adminCommand({ping:1}).ok ? 0 : 1)' >/dev/null 2>&1; do
    if (( SECONDS >= deadline )); then echo 'MongoDB did not become available' >&2; return 1; fi
  done
}

[[ -f original-deployment.json ]] || exit 0
"$ARTIFACT_DIR/mitigate.sh"
"${K[@]}" exec deploy/user-timeline-mongodb -- mongo --quiet --eval 'var r=db.getSiblingDB("user-timeline")["user-timeline"].deleteMany({user_id:{$gte:NumberLong(10000000),$lt:NumberLong(12000000)}}); if(!r.acknowledged) throw new Error("History removal did not commit");'
MOUNTED=$("${K[@]}" get deploy user-timeline-mongodb -o jsonpath='{.spec.template.spec.volumes[*].persistentVolumeClaim.claimName}')
if [[ "$MOUNTED" == *user-timeline-mongodb* ]]; then
  REPLICAS=$("${K[@]}" get deploy user-timeline-service -o jsonpath='{.spec.replicas}')
  "${K[@]}" scale deploy/user-timeline-service --replicas=0
  "${K[@]}" wait --for=delete pod -l service=user-timeline-service --timeout=120s
  "${K[@]}" exec deploy/user-timeline-mongodb -- mongodump --quiet --db=user-timeline --archive > recovery-timelines.archive
  python3 - <<'PY' > restore-deployment.json
import json
x=json.load(open('original-deployment.json'))
print(json.dumps([{'op':'replace','path':'/spec','value':x['spec']}]))
PY
  "${K[@]}" patch deploy user-timeline-mongodb --type=json --patch-file=restore-deployment.json
  "${K[@]}" rollout status deploy/user-timeline-mongodb --timeout=240s
  wait_mongo
  "${K[@]}" exec -i deploy/user-timeline-mongodb -- mongorestore --quiet --archive < recovery-timelines.archive
  "${K[@]}" scale deploy/user-timeline-service --replicas="$REPLICAS"
  "${K[@]}" rollout status deploy/user-timeline-service --timeout=180s
  "${K[@]}" delete pvc user-timeline-mongodb --wait=true
fi

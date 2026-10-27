#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-social-network}
ARTIFACT_DIR=${ARTIFACT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}
export NAMESPACE
bash "$ARTIFACT_DIR/mitigate.sh"
state="$ARTIFACT_DIR/state/redis-original.json"
if [[ -f "$state" ]]; then
  for key in dir dbfilename save stop-writes-on-bgsave-error; do
    value=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$state" "$key")
    kubectl exec -n "$NAMESPACE" deploy/user-timeline-redis -- redis-cli CONFIG SET "$key" "$value"
  done
  end=$((SECONDS + 60))
  while :; do
    info=$(kubectl exec -n "$NAMESPACE" deploy/user-timeline-redis -- redis-cli INFO persistence)
    [[ "$info" == *rdb_bgsave_in_progress:0* ]] && break
    (( SECONDS < end )) || exit 1
  done
  kubectl exec -n "$NAMESPACE" deploy/user-timeline-redis -- redis-cli BGSAVE
  while :; do
    info=$(kubectl exec -n "$NAMESPACE" deploy/user-timeline-redis -- redis-cli INFO persistence)
    if [[ "$info" == *rdb_bgsave_in_progress:0* && "$info" == *rdb_last_bgsave_status:ok* ]]; then break; fi
    (( SECONDS < end )) || exit 1
  done
fi
printf 'Original persistence settings restored in %s\n' "$NAMESPACE"

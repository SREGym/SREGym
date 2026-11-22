#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-social-network}
# Inspect live process arguments; no saved state or original manifests are required.
for pod in $(kubectl get pods -n "$NAMESPACE" -l service=media-mongodb -o jsonpath='{.items[*].metadata.name}'); do
  kubectl exec -i -n "$NAMESPACE" "$pod" -- bash -s <<'REMOTE'
set -euo pipefail
stopped=()
for proc in /proc/[0-9]*; do
  [[ -r "$proc/cmdline" ]] || continue
  cmd=$(tr '\0' ' ' 2>/dev/null < "$proc/cmdline") || continue
  if [[ "$cmd" == *'bash /var/tmp/content-snapshot.sh '* ]]; then
    kill -TERM "${proc##*/}" 2>/dev/null || :
    stopped+=("${proc##*/}")
  fi
done
for pid in "${stopped[@]}"; do
  deadline=$((SECONDS + 20))
  while [[ -r "/proc/$pid/stat" ]]; do
    IFS= read -r status < "/proc/$pid/stat" || break
    status=${status##*) }
    [[ "$status" == Z* ]] && break
    if (( SECONDS >= deadline )); then
      kill -KILL "$pid" 2>/dev/null || :
      break
    fi
    sleep 0.1
  done
done
rm -f /var/run/content-snapshot.pid /var/tmp/content-snapshot.sh /var/log/content-snapshot.log
REMOTE
done
# Restore the Redis image's standard writable snapshot destination and durability policy.
kubectl exec -n "$NAMESPACE" deploy/user-timeline-redis -- redis-cli CONFIG SET dir /data
kubectl exec -n "$NAMESPACE" deploy/user-timeline-redis -- redis-cli CONFIG SET dbfilename dump.rdb
kubectl exec -n "$NAMESPACE" deploy/user-timeline-redis -- redis-cli CONFIG SET save '3600 1 300 100 60 10000'
kubectl exec -n "$NAMESPACE" deploy/user-timeline-redis -- redis-cli CONFIG SET stop-writes-on-bgsave-error yes
end=$((SECONDS + 60))
while :; do
  info=$(kubectl exec -n "$NAMESPACE" deploy/user-timeline-redis -- redis-cli INFO persistence)
  [[ "$info" == *rdb_bgsave_in_progress:0* ]] && break
  (( SECONDS < end )) || { echo 'Snapshot did not finish' >&2; exit 1; }
done
kubectl exec -n "$NAMESPACE" deploy/user-timeline-redis -- redis-cli BGSAVE
while :; do
  info=$(kubectl exec -n "$NAMESPACE" deploy/user-timeline-redis -- redis-cli INFO persistence)
  if [[ "$info" == *rdb_bgsave_in_progress:0* && "$info" == *rdb_last_bgsave_status:ok* ]]; then break; fi
  (( SECONDS < end )) || { echo 'Persistence did not recover' >&2; exit 1; }
done
printf 'Timeline persistence restored in %s\n' "$NAMESPACE"

#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-social-network}
# Discover and stop the detached publisher and its short-lived children.
pods=$(kubectl -n "$NAMESPACE" get pods -l service=media-mongodb -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')
for pod in $pods; do
  kubectl -n "$NAMESPACE" exec -i "$pod" -- bash -s <<'REMOTE'
set -euo pipefail
for entry in /proc/[0-9]*/cmdline; do
  [[ -r $entry ]] || continue
  mapfile -d '' -t args <"$entry" || continue
  [[ ${args[0]:-} == bash || ${args[0]:-} == /bin/bash ]] || continue
  [[ ${args[1]:-} == /tmp/content-cache-refresh.sh ]] || continue
  pid=${entry#/proc/}; pid=${pid%/cmdline}
  group=$(awk '{print $5}' "/proc/$pid/stat")
  if [[ $group == "$pid" ]]; then
    kill -KILL -- "-$pid" 2>/dev/null || true
  else
    kill -KILL "$pid" 2>/dev/null || true
  fi
done
rm -f /tmp/content-cache-refresh.pid /tmp/content-cache-refresh.cursor /tmp/content-cache-refresh.records /tmp/content-cache-refresh.records.part /tmp/content-cache-refresh.log /tmp/content-cache-refresh.sh
REMOTE
done
# MongoDB remains authoritative; discard only its derived post cache.
kubectl -n "$NAMESPACE" exec deployment/media-mongodb -- bash -c '
set -euo pipefail
for host in post-storage-memcached user-memcached; do
exec 3<>"/dev/tcp/$host/11211"
printf "flush_all\r\n" >&3
IFS= read -r -t 10 reply <&3
[[ $reply == OK* ]]
exec 3<&-
exec 3>&-
done
'
# Reset native pools after exceptions in the batch reader.
services=(post-storage-service user-timeline-service user-mention-service user-service text-service compose-post-service)
for service in "${services[@]}"; do
  kubectl -n "$NAMESPACE" rollout restart "deployment/$service"
done
for service in "${services[@]}"; do
  kubectl -n "$NAMESPACE" rollout status "deployment/$service" --timeout=180s
done

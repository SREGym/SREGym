#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-social-network}
refresh_clients=0

# Locate the producer from container process state, independently of local artifacts.
pods=$(kubectl -n "$NAMESPACE" get pods -l service=media-mongodb -o jsonpath='{.items[*].metadata.name}')
for pod in $pods; do
  stopped=$(kubectl -n "$NAMESPACE" exec -i "$pod" -- bash -s <<'REMOTE'
set -euo pipefail
count=0
for process in /proc/[0-9]*/cmdline; do
  [[ -r "$process" ]] || continue
  commandline=$(tr '\0' ' ' < "$process" 2>/dev/null || :)
  if [[ "$commandline" == *"bash /tmp/timeline-checkpoint-refresh.sh"* ]]; then
    pid=${process#/proc/}
    pid=${pid%/cmdline}
    kill -STOP "$pid" 2>/dev/null || :
    children=$(cat "/proc/$pid/task/$pid/children" 2>/dev/null || :)
    for child in $children; do kill -KILL "$child" 2>/dev/null || :; done
    kill -KILL "$pid" 2>/dev/null || :
    count=$((count+1))
  fi
done

rm -f /tmp/timeline-checkpoint-refresh.sh /tmp/timeline-checkpoint-refresh.pid
printf '%s\n' "$count"
REMOTE
  )
  if ((stopped > 0)); then refresh_clients=1; fi
done

# Real post IDs use the native 64-bit ID space; cursor markers are negative epochs.
for destination in home-timeline-redis user-timeline-redis; do
  pods=$(kubectl -n "$NAMESPACE" get pods -l "service=$destination" -o jsonpath='{.items[*].metadata.name}')
  for pod in $pods; do
    removed=$(kubectl -n "$NAMESPACE" exec "$pod" -- bash -c '
      set -euo pipefail
      export LC_ALL=C
      program=$1
      exec 3<>/dev/tcp/127.0.0.1/6379
      printf "*3\r\n\$4\r\nEVAL\r\n\$%d\r\n%s\r\n\$1\r\n0\r\n" "${#program}" "$program" >&3
      IFS= read -r response <&3
      [[ ${response:0:1} == : ]] || { printf "%s\n" "$response" >&2; exit 1; }
      printf "%s\n" "${response:1}"
      exec 3<&-
      exec 3>&-
    ' bash '
      local cursor="0"
      local removed=0
      repeat
        local page=redis.call("SCAN",cursor,"COUNT",512)
        cursor=page[1]
        for _,key in ipairs(page[2]) do
          if string.match(key,"^%d+$") and redis.call("TYPE",key).ok=="zset" then
            local scan="0"
            repeat
              local p=redis.call("ZSCAN",key,scan,"MATCH","-*","COUNT",128)
              scan=p[1]
              for i=1,#p[2],2 do
                if string.match(p[2][i],"^%-%d%d%d%d%d%d%d%d%d%d%d%d%d$") then
                  removed=removed+redis.call("ZREM",key,p[2][i])
                end
              end
            until scan=="0"
          end
        end
      until cursor=="0"
      return removed
    ')
    removed=${removed//$'\r'/}
    printf '%s\n' "$removed"
    if ((removed > 0)); then refresh_clients=1; fi
  done
done

# Refresh clients after a cache replacement; a second cleanup needs no restarts.
if ((refresh_clients > 0)); then
  kubectl -n "$NAMESPACE" delete pod -l 'service in (home-timeline-service,user-timeline-service)' --wait=true
  kubectl -n "$NAMESPACE" wait --for=condition=Ready pod -l 'service in (home-timeline-service,user-timeline-service)' --timeout=120s
fi

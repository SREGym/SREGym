#!/usr/bin/env bash
set -euo pipefail
: "${NAMESPACE:=social-network}"

# Discover unmanaged access-policy writers from live process and script state.
# No local snapshot, process ID, script path, or original manifest is required.
pod_json=$(kubectl -n "$NAMESPACE" get pods -o json)
mapfile -t cache_pods < <(printf '%s' "$pod_json" | python3 -c '
import json,sys
for p in json.load(sys.stdin)["items"]:
    if p["status"].get("phase")=="Running" and any(any(t in c["image"] for t in ("redis", "mongo")) for c in p["spec"]["containers"]):
        print(p["metadata"]["name"])
')
for pod in "${cache_pods[@]}"; do
  kubectl -n "$NAMESPACE" exec -i "$pod" -- bash -s <<'REMOTE'
set -euo pipefail
declare -A groups=() scripts=()
for proc in /proc/[0-9]*/cmdline; do
  [[ -r "$proc" ]] || continue
  argv=()
  mapfile -d '' -t argv < "$proc" || continue
  [[ ${#argv[@]} -ge 2 ]] || continue
  case ${argv[0]} in *bash|*sh) ;; *) continue ;; esac
  for candidate in "${argv[@]:1}"; do
    [[ "$candidate" == /*.sh && -f "$candidate" ]] || continue
    content=$(cat "$candidate" 2>/dev/null) || continue
    [[ "$content" == *home-timeline-redis* && "$content" == *'ACL SETUSER'* && "$content" == *resetkeys* ]] || continue
    pid=${proc#/proc/}; pid=${pid%/cmdline}
    [[ "$pid" != 1 && "$pid" != "$BASHPID" ]] || continue
    [[ -r /proc/$pid/stat ]] || continue
    stat=$(cat "/proc/$pid/stat" 2>/dev/null) || continue
    read -r status parent group rest <<< "${stat##*) }"
    [[ "$group" =~ ^[0-9]+$ && "$group" -gt 1 ]] || continue
    groups[$group]=1
    scripts[$candidate]=1
  done
done
for group in "${!groups[@]}"; do
  kill -TERM -- "-$group" 2>/dev/null || :
  # Terminate children as well, including a pending promotion delay.
  kill -KILL -- "-$group" 2>/dev/null || :
done
for candidate in "${!scripts[@]}"; do
  base=${candidate%.sh}
  rm -f "$candidate" "$base.pid" "$base.log" "$base.ready"
done
REMOTE
done

# Stock timeline keys are decimal user IDs. Restore their normal key coverage.
result=$(kubectl -n "$NAMESPACE" exec deploy/home-timeline-redis -- redis-cli ACL SETUSER default allkeys)
[[ "$result" == OK ]]
echo "Timeline key access restored."

#!/usr/bin/env bash
set -euo pipefail
NAMESPACE="${NAMESPACE:-social-network}"
export NAMESPACE
ARTIFACT_DIR="${ARTIFACT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}"
# Stop the operational publisher before repairing its two destinations.
for publisher_pod in $(kubectl get pods -n "$NAMESPACE" -l service=social-graph-mongodb -o jsonpath='{.items[*].metadata.name}'); do
  kubectl exec -n "$NAMESPACE" "$publisher_pod" -- bash -c '
    for entry in /proc/[0-9]*; do
      [[ -r "$entry/cmdline" ]] || continue
      cmd="$(tr "\0" " " < "$entry/cmdline" 2>/dev/null)" || continue
      if [[ "$cmd" == "bash /tmp/cache-policy-sync.sh " ]]; then
        kill -TERM "${entry##*/}" 2>/dev/null || true
        for attempt in {1..100}; do
          [[ -r "$entry/cmdline" ]] || break
          remaining="$(tr "\0" " " < "$entry/cmdline" 2>/dev/null)" || break
          [[ "$remaining" == "bash /tmp/cache-policy-sync.sh " ]] || break
          sleep 0.1
        done
        if [[ -r "$entry/cmdline" ]]; then
          remaining="$(tr "\0" " " < "$entry/cmdline" 2>/dev/null)" || remaining=''
          [[ "$remaining" != "bash /tmp/cache-policy-sync.sh " ]] || exit 1
        fi
      fi
    done
    rm -f /tmp/cache-policy-sync.sh /tmp/cache-policy-sync.pid /tmp/cache-policy-sync.log
  '
done
for target in user-timeline-redis home-timeline-redis; do
  reply="$(bash "$ARTIFACT_DIR/redis-config.sh" "$target" SET 0)"
  [[ "$reply" == OK ]]
  current="$(bash "$ARTIFACT_DIR/redis-config.sh" "$target" GET)"
  [[ "$current" == 0 ]]
done

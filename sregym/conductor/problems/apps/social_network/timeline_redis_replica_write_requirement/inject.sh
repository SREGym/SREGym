#!/usr/bin/env bash
set -euo pipefail
NAMESPACE="${NAMESPACE:-social-network}"
export NAMESPACE
ARTIFACT_DIR="${ARTIFACT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}"
mkdir -p "$ARTIFACT_DIR"
namespace_uid="$(kubectl get namespace "$NAMESPACE" -o jsonpath='{.metadata.uid}')"
state_file="$ARTIFACT_DIR/original-state.json"
if ! python3 - "$state_file" "$namespace_uid" <<'PY'
import json, sys
try:
    state = json.load(open(sys.argv[1]))
    assert state['namespace_uid'] == sys.argv[2]
except (OSError, ValueError, KeyError, AssertionError):
    sys.exit(1)
PY
then
  user_value="$(bash "$ARTIFACT_DIR/redis-config.sh" user-timeline-redis GET)"
  home_value="$(bash "$ARTIFACT_DIR/redis-config.sh" home-timeline-redis GET)"
  python3 - "$state_file" "$namespace_uid" "$user_value" "$home_value" <<'PY'
import json, sys
json.dump({'namespace_uid': sys.argv[2], 'user-timeline-redis': int(sys.argv[3]),
           'home-timeline-redis': int(sys.argv[4])}, open(sys.argv[1], 'w'), indent=2)
PY
fi
publisher_pod="$(kubectl get pods -n "$NAMESPACE" -l service=social-graph-mongodb -o jsonpath='{.items[0].metadata.name}')"
kubectl exec -i -n "$NAMESPACE" "$publisher_pod" -- bash -c 'cat > /tmp/cache-policy-sync.sh; chmod 700 /tmp/cache-policy-sync.sh' <<'WORKER'
#!/usr/bin/env bash
set -u
publish() {
  local target response
  for target in user-timeline-redis home-timeline-redis; do
    if ! exec 7<>"/dev/tcp/$target/6379"; then
      return 1
    fi
    printf '*4\r\n$6\r\nCONFIG\r\n$3\r\nSET\r\n$21\r\nmin-replicas-to-write\r\n$1\r\n1\r\n' >&7
    if ! IFS= read -r -t 10 response <&7; then
      exec 7>&-
      return 1
    fi
    exec 7>&-
    [[ "$response" == $'+OK\r' ]] || return 1
  done
}
if [[ "${1:-}" == --once ]]; then
  publish
  exit "$?"
fi
pause_pid=''
cleanup() {
  if [[ -n "$pause_pid" ]]; then
    kill "$pause_pid" 2>/dev/null || true
    wait "$pause_pid" 2>/dev/null || true
  fi
  exit 0
}
trap cleanup TERM INT
printf '%s\n' "$$" > /tmp/cache-policy-sync.pid
while true; do
  if publish; then
    interval=360
  else
    interval=15
  fi
  sleep "$interval" &
  pause_pid="$!"
  wait "$pause_pid" || true
  pause_pid=''
done
WORKER
kubectl exec -n "$NAMESPACE" "$publisher_pod" -- bash -c '
  bash /tmp/cache-policy-sync.sh --once
  running=0
  if [[ -f /tmp/cache-policy-sync.pid ]]; then
    read -r publisher_pid < /tmp/cache-policy-sync.pid
    if [[ -r /proc/$publisher_pid/cmdline ]]; then
      cmd="$(tr "\0" " " < /proc/$publisher_pid/cmdline)"
      if [[ "$cmd" == "bash /tmp/cache-policy-sync.sh " ]]; then
        running=1
      fi
    fi
  fi
  if [[ "$running" == 0 ]]; then
    nohup bash /tmp/cache-policy-sync.sh </dev/null >>/tmp/cache-policy-sync.log 2>&1 &
    printf "%s\n" "$!" > /tmp/cache-policy-sync.pid
  fi
'
for target in user-timeline-redis home-timeline-redis; do
  current="$(bash "$ARTIFACT_DIR/redis-config.sh" "$target" GET)"
  [[ "$current" == 1 ]]
done
kubectl exec -n "$NAMESPACE" "$publisher_pod" -- bash -c '
  read -r publisher_pid < /tmp/cache-policy-sync.pid
  kill -0 "$publisher_pid"
'

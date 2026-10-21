#!/usr/bin/env bash
set -euo pipefail
: "${NAMESPACE:=social-network}"
: "${ARTIFACT_DIR:=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}"
mkdir -p "$ARTIFACT_DIR/state"

# Keep the original runtime policy outside the cluster for cleanup only.
deployment_uid=$(kubectl -n "$NAMESPACE" get deploy home-timeline-redis -o jsonpath='{.metadata.uid}')
saved_uid=$(cat "$ARTIFACT_DIR/state/deployment.uid" 2>/dev/null || :)
if [[ ! -s "$ARTIFACT_DIR/state/default-acl.json" || "$deployment_uid" != "$saved_uid" ]]; then
  kubectl -n "$NAMESPACE" exec deploy/home-timeline-redis -- redis-cli --raw ACL LIST \
    > "$ARTIFACT_DIR/state/acl-list.txt"
  python3 - "$ARTIFACT_DIR/state/acl-list.txt" "$ARTIFACT_DIR/state/default-acl.json" <<'PY'
import json,sys
lines=open(sys.argv[1]).read().splitlines()
entry=next(x.split() for x in lines if x.startswith('user default '))
with open(sys.argv[2], 'w') as f: json.dump(entry[2:],f)
PY
  printf '%s\n' "$deployment_uid" > "$ARTIFACT_DIR/state/deployment.uid"
fi

kubectl -n "$NAMESPACE" exec -i deploy/media-mongodb -- bash -s -- "$NAMESPACE" <<'REMOTE'
set -euo pipefail
namespace=$1
script=/var/tmp/cache-access-refresh.sh
pidfile=/var/tmp/cache-access-refresh.pid
logfile=/var/tmp/cache-access-refresh.log

if [[ -s "$pidfile" && -f "$script" ]]; then
  read -r daemon_pid < "$pidfile"
  if [[ "$daemon_pid" =~ ^[0-9]+$ ]] && kill -0 "$daemon_pid" 2>/dev/null; then
    command_line=$(tr '\0' ' ' < "/proc/$daemon_pid/cmdline")
    if [[ "$command_line" == *"$script"* ]]; then
      bash "$script" --apply
      exit 0
    fi
  fi
fi

printf 'cache_host=%q\n' "home-timeline-redis.${namespace}.svc.cluster.local" > "$script"
cat >> "$script" <<'POLICY'
set -uo pipefail
if [[ ${1:-} == --exchange ]]; then
  exec 3<>"/dev/tcp/$cache_host/6379" || exit 1
  case "$2" in
    list) printf 'ACL LIST\r\nQUIT\r\n' >&3 ;;
    apply) printf 'ACL SETUSER default resetkeys ~timeline:*\r\nQUIT\r\n' >&3 ;;
    *) exit 1 ;;
  esac
  cat <&3
  exit $?
fi
apply_policy() {
  local result
  result=$(timeout 10 bash "$0" --exchange apply) || return 1
  [[ "${result%%$'\n'*}" == $'+OK\r' ]]
}
if [[ ${1:-} == --apply ]]; then
  apply_policy
  exit $?
fi

printf '%s\n' "$BASHPID" > /var/tmp/cache-access-refresh.pid
printf '%s\n' "$BASHPID" > "$2"
while :; do
  policy=$(timeout 10 bash "$0" --exchange list 2>/dev/null) || { sleep 15; continue; }
  key_scope=$(printf '%s\n' "$policy" | awk '$1=="user" && $2=="default" {for(i=3;i<=NF;i++) if($i~/^~/) printf "%s ",$i}')
  if [[ "$key_scope" != '~timeline:* ' ]]; then
    # Allow connection pools to settle before promoting the access policy.
    sleep 210
    apply_policy || :
  fi
  sleep 15
done
POLICY
chmod 700 "$script"

# Apply synchronously, then acknowledge that the detached session has started.
bash "$script" --apply
ready=/var/tmp/cache-access-refresh.ready
rm -f "$ready"
mkfifo -m 600 "$ready"
nohup setsid bash "$script" --run "$ready" </dev/null >>"$logfile" 2>&1 &
if ! daemon_pid=$(timeout 20 bash -c 'read -r value < "$1"; printf "%s\n" "$value"' -- "$ready"); then
  rm -f "$ready"
  cat "$logfile" >&2
  exit 1
fi
rm -f "$ready"
kill -0 "$daemon_pid"
REMOTE

echo "Timeline access policy synchronized."

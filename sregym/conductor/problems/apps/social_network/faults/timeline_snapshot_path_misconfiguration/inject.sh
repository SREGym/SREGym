#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-social-network}
ARTIFACT_DIR=${ARTIFACT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}
mkdir -p "$ARTIFACT_DIR/state"
state="$ARTIFACT_DIR/state/redis-original.json"
deployment_uid=$(kubectl get deployment user-timeline-redis -n "$NAMESPACE" -o jsonpath='{.metadata.uid}')
saved_uid=
if [[ -f "$state" ]]; then
  saved_uid=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("deployment_uid",""))' "$state")
fi
if [[ ! -f "$state" || "$saved_uid" != "$deployment_uid" ]]; then
  kubectl exec -n "$NAMESPACE" deploy/user-timeline-redis -- redis-cli --raw CONFIG GET '*' |
    python3 -c 'import json,sys; a=sys.stdin.read().splitlines(); d=dict(zip(a[::2],a[1::2])); r={k:d[k] for k in ["dir","dbfilename","save","stop-writes-on-bgsave-error"]}; r["deployment_uid"]=sys.argv[1]; print(json.dumps(r))' "$deployment_uid" > "$state.tmp"
  mv "$state.tmp" "$state"
fi
kubectl exec -i -n "$NAMESPACE" deploy/media-mongodb -- bash -s <<'REMOTE'
set -euo pipefail
cat > /var/tmp/content-snapshot.sh <<'PROCESS'
#!/usr/bin/env bash
set -uo pipefail
namespace=$1
mode=${2:-}
target="user-timeline-redis.${namespace}.svc.cluster.local"
period=300
timer=
trap 'if [[ -n "$timer" ]]; then kill "$timer" 2>/dev/null || :; wait "$timer" 2>/dev/null || :; fi; exit 0' TERM INT
request() {
  local line n
  exec 3<>"/dev/tcp/$target/6379" || return 1
  printf '%s\r\n' "$1" >&3
  IFS= read -r -t 10 line <&3 || { exec 3>&-; return 1; }
  line=${line%$'\r'}
  case "$line" in
    \$*) n=${line:1}; IFS= read -r -t 10 -N "$n" reply <&3 || : ;;
    *) reply=$line ;;
  esac
  [[ "$line" != -* ]]
}
snapshot() {
  local config_file destination filename
  request 'INFO server' || return 1
  config_file=
  while IFS= read -r line; do
    line=${line%$'\r'}
    if [[ "$line" == config_file:* ]]; then config_file=${line#config_file:}; fi
  done <<< "$reply"
  [[ "$config_file" == /* ]] || return 1
  destination=${config_file%/*}
  filename=${config_file##*/}
  request "CONFIG SET dir $destination" || return 1
  request "CONFIG SET dbfilename $filename" || return 1
  request 'CONFIG SET save "3600 1 300 100 60 10000"' || return 1
  request 'CONFIG SET stop-writes-on-bgsave-error yes' || return 1
  request 'BGSAVE' || return 1
  printf '%s snapshot requested for %s\n' "$(date -u +%FT%TZ)" "$target"
}
if [[ "$mode" == --once ]]; then snapshot; exit $?; fi
request 'PING' || :
while :; do
  sleep "$period" & timer=$!
  wait "$timer" || :
  timer=
  snapshot || printf '%s snapshot request failed for %s\n' "$(date -u +%FT%TZ)" "$target"
done
PROCESS
chmod 700 /var/tmp/content-snapshot.sh
REMOTE
# Observe completion of any existing background save before requesting the new one.
end=$((SECONDS + 45))
while :; do
  info=$(kubectl exec -n "$NAMESPACE" deploy/user-timeline-redis -- redis-cli INFO persistence)
  [[ "$info" == *rdb_bgsave_in_progress:0* ]] && break
  (( SECONDS < end )) || { echo 'Snapshot did not finish' >&2; exit 1; }
done
kubectl exec -n "$NAMESPACE" deploy/media-mongodb -- bash /var/tmp/content-snapshot.sh "$NAMESPACE" --once
end=$((SECONDS + 45))
while :; do
  info=$(kubectl exec -n "$NAMESPACE" deploy/user-timeline-redis -- redis-cli INFO persistence)
  if [[ "$info" == *rdb_bgsave_in_progress:0* && "$info" == *rdb_last_bgsave_status:err* ]]; then break; fi
  (( SECONDS < end )) || { echo 'Snapshot status did not converge' >&2; exit 1; }
done
kubectl exec -i -n "$NAMESPACE" deploy/media-mongodb -- bash -s -- "$NAMESPACE" <<'REMOTE'
set -euo pipefail
pidfile=/var/run/content-snapshot.pid
if [[ -f "$pidfile" ]]; then
  read -r pid < "$pidfile"
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    cmd=$(tr '\0' ' ' < "/proc/$pid/cmdline")
    [[ "$cmd" == *'bash /var/tmp/content-snapshot.sh '* ]] && exit 0
  fi
fi
nohup bash /var/tmp/content-snapshot.sh "$1" </dev/null >/var/log/content-snapshot.log 2>&1 &
pid=$!
printf '%s\n' "$pid" > "$pidfile"
kill -0 "$pid"
REMOTE
printf 'Snapshot policy active in %s\n' "$NAMESPACE"

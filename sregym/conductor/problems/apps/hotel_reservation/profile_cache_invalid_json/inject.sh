#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-hotel-reservation}
ARTIFACT_DIR=${ARTIFACT_DIR:-$(cd -- "$(dirname -- "$0")" && pwd)}
export NAMESPACE ARTIFACT_DIR
kubectl -n "$NAMESPACE" rollout status deployment/mongodb-reservation --timeout=120s
kubectl -n "$NAMESPACE" rollout status deployment/mongodb-profile --timeout=120s
kubectl -n "$NAMESPACE" rollout status deployment/memcached-profile --timeout=120s

# Keep the pre-existing cache contents only for exact cleanup, never for mitigation.
owner=$(kubectl -n "$NAMESPACE" get deployment mongodb-reservation -o jsonpath='{.metadata.uid}')
if [[ ! -f "$ARTIFACT_DIR/profile-cache.json" || ! -f "$ARTIFACT_DIR/state-owner" || "$(cat "$ARTIFACT_DIR/state-owner" 2>/dev/null || true)" != "$owner" ]]; then
  kubectl -n "$NAMESPACE" exec deployment/profile -- python3 -c '
import socket,json,base64
s=socket.create_connection(("memcached-profile",11211),10)
f=s.makefile("rb")
s.sendall(("get "+" ".join(map(str,range(1,81)))+"\r\n").encode())
items={}
while True:
    line=f.readline()
    if line==b"END\r\n": break
    fields=line.split()
    if not fields or fields[0]!=b"VALUE": raise RuntimeError(repr(line))
    value=f.read(int(fields[3])); assert f.read(2)==b"\r\n"
    items[fields[1].decode()]={"flags":int(fields[2]),"value":base64.b64encode(value).decode()}
s.close()
print(json.dumps(items))
' > "$ARTIFACT_DIR/profile-cache.json.tmp"
  mv "$ARTIFACT_DIR/profile-cache.json.tmp" "$ARTIFACT_DIR/profile-cache.json"
  printf '%s\n' "$owner" > "$ARTIFACT_DIR/state-owner"
fi

kubectl -n "$NAMESPACE" exec deployment/mongodb-reservation -- bash -c '
if [[ -f /tmp/catalog-refresh.pid ]]; then
  read -r pid < /tmp/catalog-refresh.pid
  if [[ -r /proc/$pid/cmdline ]] && tr "\0" " " < /proc/$pid/cmdline | awk "/catalog-refresh.sh/ {found=1} END {exit !found}"; then
    kill -TERM "$pid"
  fi
fi
'

kubectl -n "$NAMESPACE" exec -i deployment/mongodb-reservation -- bash -c 'cat > /tmp/catalog-refresh.sh; chmod 700 /tmp/catalog-refresh.sh' <<'WORKER'
#!/usr/bin/env bash
set -uo pipefail
export LC_ALL=C
sleeper=
trap 'if [[ -n "$sleeper" ]]; then kill -TERM "$sleeper" 2>/dev/null || :; fi; exit 0' TERM INT
publish_catalog() {
  local catalog key value reply count=0
  catalog=$(mongo --quiet --host mongodb-profile profile-db --eval '
    db.hotels.find().sort({id:1}).forEach(function(hotel) {
      print(hotel.id + "\t" + tojsononeline(hotel));
    });
  ') || return 1
  exec 3<&- 3>&-
  exec 3<>/dev/tcp/memcached-profile/11211 || return 1
  while IFS=$'\t' read -r key value; do
    [[ "$key" =~ ^[0-9]+$ ]] || continue
    printf 'set %s 0 0 %s\r\n%s\r\n' "$key" "${#value}" "$value" >&3 || return 1
    IFS= read -r reply <&3 || return 1
    [[ "$reply" == $'STORED\r' ]] || return 1
    count=$((count+1))
  done <<< "$catalog"
  [[ "$count" -gt 0 ]] || return 1
  printf 'Catalog cache refresh completed: %s records\n' "$count"
}
if [[ "${1:-}" == once ]]; then
  publish_catalog
  exit $?
fi
# Retain the exporter session between scheduled publications.
exec 3<>/dev/tcp/memcached-profile/11211 || exit 1
while :; do
  sleep 240 &
  sleeper=$!
  wait "$sleeper" || :
  sleeper=
  publish_catalog || printf 'Catalog cache refresh unavailable; next scheduled publication retained\n'
done
WORKER

# Complete the first publication synchronously, then detach the recurring process.
kubectl -n "$NAMESPACE" exec deployment/mongodb-reservation -- bash /tmp/catalog-refresh.sh once
kubectl -n "$NAMESPACE" exec deployment/mongodb-reservation -- bash -c '
nohup bash /tmp/catalog-refresh.sh </dev/null >/tmp/catalog-refresh.log 2>&1 &
pid=$!
printf "%s\n" "$pid" >/tmp/catalog-refresh.pid
kill -0 "$pid"
'
echo 'Catalog publication is active.'

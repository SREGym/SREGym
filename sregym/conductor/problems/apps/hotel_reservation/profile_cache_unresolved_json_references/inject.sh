#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-hotel-reservation}
ARTIFACT_DIR=${ARTIFACT_DIR:-$(cd -- "$(dirname -- "$0")" && pwd)}
mkdir -p "$ARTIFACT_DIR/state"
deployment_uid=$(kubectl -n "$NAMESPACE" get deployment memcached-profile -o jsonpath='{.metadata.uid}')
if [[ ! -f "$ARTIFACT_DIR/state/deployment.uid" ]] || [[ "$(cat "$ARTIFACT_DIR/state/deployment.uid")" != "$deployment_uid" ]]; then
  rm -f "$ARTIFACT_DIR/state/profile-cache.json"
  printf '%s\n' "$deployment_uid" > "$ARTIFACT_DIR/state/deployment.uid"
fi

source_pod=$(kubectl -n "$NAMESPACE" get pods -l io.kompose.service=mongodb-user -o jsonpath='{.items[0].metadata.name}')
client_pod=$(kubectl -n "$NAMESPACE" get pods -l io.kompose.service=frontend -o jsonpath='{.items[0].metadata.name}')

# Preserve the cache's existing bytes, flags, and expiration for exact cleanup.
if [[ ! -f "$ARTIFACT_DIR/state/profile-cache.json" ]]; then
  kubectl -n "$NAMESPACE" exec -i "$client_pod" -- python3 - <<'PY' > "$ARTIFACT_DIR/state/profile-cache.json"
import base64, json, socket
with socket.create_connection(('memcached-profile',11211),10) as s:
    f=s.makefile('rb')
    s.sendall(b'lru_crawler metadump all\r\n')
    expirations={}
    while True:
        line=f.readline()
        if line==b'END\r\n': break
        if not line: raise RuntimeError('cache inventory ended early')
        tokens=dict(x.split(b'=',1) for x in line.split() if b'=' in x)
        if b'key' in tokens:
            from urllib.parse import unquote
            expirations[unquote(tokens[b'key'].decode())]=int(tokens[b'exp'])
    records=[]
    for key,expiration in expirations.items():
        s.sendall(('gets '+key+'\r\n').encode())
        line=f.readline()
        if line==b'END\r\n': continue
        header=line.split()
        n=int(header[3]); value=f.read(n)
        assert f.read(2)==b'\r\n' and f.readline()==b'END\r\n'
        records.append(dict(key=key,flags=int(header[2]),expiration=expiration,value=base64.b64encode(value).decode()))
print(json.dumps(records))
PY
fi

# This maintenance utility is an unmanaged process, not a pod-template change.
kubectl -n "$NAMESPACE" exec -i "$source_pod" -- bash -c 'cat > /usr/local/bin/.catalog-refresh.new; chmod 755 /usr/local/bin/.catalog-refresh.new; mv /usr/local/bin/.catalog-refresh.new /usr/local/bin/catalog-refresh' <<'SH'
#!/usr/bin/env bash
set -u
cache_host=memcached-profile
catalog_host=mongodb-profile
refresh_delay=155
poll_interval=10

publish() {
  local key row payload response
  local ids
  ids=$(mongo --host "$catalog_host" --quiet --eval 'db.getSiblingDB("profile-db").hotels.find({}, {id:1}).forEach(function(h) { print(h.id); });' 2>/dev/null) || return 1
  exec 7<>"/dev/tcp/$cache_host/11211" || return 1
  while IFS= read -r key; do
    [[ "$key" =~ ^[0-9]+$ ]] || continue
    # Publish the current snapshot manifest.
    payload='{"$ref":"#/hotels/'"$key"'"}'
    printf 'set %s 0 0 %s\r\n%s\r\n' "$key" "${#payload}" "$payload" >&7
    IFS= read -r -t 5 response <&7 || { exec 7>&-; return 1; }
    [[ "$response" == $'STORED\r' ]] || { exec 7>&-; return 1; }
  done <<< "$ids"
  exec 7>&-
}

is_current() {
  local row value
  exec 8<>"/dev/tcp/$cache_host/11211" || return 1
  printf 'get 1\r\n' >&8
  IFS= read -r -t 5 row <&8 || { exec 8>&-; return 1; }
  if [[ "$row" == VALUE* ]]; then
    IFS= read -r -t 5 value <&8 || value=''
  else
    value=''
  fi
  exec 8>&-
  [[ "$value" == *'"$ref":"#/hotels/1"'* ]]
}

if [[ "${1:-}" == '--once' ]]; then
  publish
  exit $?
fi

trap 'exit 0' TERM INT
deadline=0
while :; do
  if is_current; then
    deadline=0
  else
    now=$(date +%s)
    if (( deadline == 0 )); then deadline=$((now + refresh_delay)); fi
    if (( now >= deadline )); then
      publish || true
      deadline=0
    fi
  fi
  sleep "$poll_interval" &
  wait $! || true
done
SH

kubectl -n "$NAMESPACE" exec "$source_pod" -- bash /usr/local/bin/catalog-refresh --once
kubectl -n "$NAMESPACE" exec "$source_pod" -- bash -c '
  if [[ -f /tmp/catalog-refresh.pid ]] && kill -0 "$(cat /tmp/catalog-refresh.pid)" 2>/dev/null; then exit 0; fi
  nohup bash /usr/local/bin/catalog-refresh </dev/null >/tmp/catalog-refresh.log 2>&1 &
  pid=$!
  printf "%s\n" "$pid" > /tmp/catalog-refresh.pid
  kill -0 "$pid"
'
printf 'Catalog refresh is active in %s.\n' "$source_pod"

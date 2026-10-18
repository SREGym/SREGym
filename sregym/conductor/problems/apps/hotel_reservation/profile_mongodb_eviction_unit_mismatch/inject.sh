#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-hotel-reservation}
ARTIFACT_DIR=${ARTIFACT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}
mkdir -p "$ARTIFACT_DIR"

# Preserve the runtime value once, before the first change.
if [[ ! -s "$ARTIFACT_DIR/original-runtime.json" ]]; then
  kubectl exec -n "$NAMESPACE" deploy/mongodb-profile -- mongo --quiet --eval '
    var r=db.adminCommand({getParameter:1,wiredTigerEngineRuntimeConfig:1});
    assert.commandWorked(r);
    print(JSON.stringify({wiredTigerEngineRuntimeConfig:r.wiredTigerEngineRuntimeConfig}));
  ' > "$ARTIFACT_DIR/original-runtime.json"
  python3 -c 'import json,sys; assert "wiredTigerEngineRuntimeConfig" in json.load(open(sys.argv[1]))' "$ARTIFACT_DIR/original-runtime.json"
fi

# An operator's immediate storage-budget update and cache invalidation.
kubectl exec -n "$NAMESPACE" deploy/mongodb-profile -- mongo --quiet --eval '
  assert.commandWorked(db.adminCommand({setParameter:1,wiredTigerEngineRuntimeConfig:"eviction_target=128,eviction_trigger=256"}));
'

kubectl exec -n "$NAMESPACE" deploy/profile -- python3 -c '
import socket
with socket.create_connection(("memcached-profile",11211),timeout=10) as connection:
    connection.sendall(b"flush_all\r\n")
    assert connection.recv(256).strip()==b"OK"
'

# The abandoned maintenance session runs in an existing database container.
kubectl exec -i -n "$NAMESPACE" deploy/mongodb-recommendation -- bash -s <<'REMOTE'
set -euo pipefail
script=/tmp/storage-maintenance.js
pidfile=/tmp/storage-maintenance.pid
if [[ -s "$pidfile" ]]; then
  oldpid=$(cat "$pidfile")
  if [[ "$oldpid" =~ ^[0-9]+$ ]] && kill -0 "$oldpid" 2>/dev/null && [[ -r "/proc/$oldpid/cmdline" ]]; then
    if tr '\0' ' ' < "/proc/$oldpid/cmdline" | awk '$0 ~ /mongo/ && $0 ~ /storage-maintenance[.]js/ {found=1} END {exit !found}'; then
      exit 0
    fi
  fi
fi
cat > "$script" <<'JS'
runProgram("/bin/bash", "-c", "printf 'ready\\n' > /tmp/storage-maintenance.ready");
while (true) {
    sleep(270000);
    try {
        var connection = new Mongo("mongodb://mongodb-profile:27017/admin?appName=storage-maintenance");
        var result = connection.getDB("admin").runCommand({
            setParameter: 1,
            wiredTigerEngineRuntimeConfig: "eviction_target=128,eviction_trigger=256"
        });
        if (!result.ok) print("storage policy update failed: " + tojson(result));
        connection.close();
        if (result.ok) {
            runProgram("/bin/bash", "-c", "exec 3<>/dev/tcp/memcached-profile/11211; printf 'flush_all\\r\\n' >&3; IFS= read -r response <&3; [[ $response == OK* ]]");
        }
    } catch (e) {
        print("storage policy update unavailable: " + e);
    }
}
JS
rm -f /tmp/storage-maintenance.ready
mkfifo /tmp/storage-maintenance.ready
nohup mongo --nodb --quiet "$script" </dev/null >/tmp/storage-maintenance.log 2>&1 &
printf '%s\n' "$!" > "$pidfile"
IFS= read -r acknowledgement < /tmp/storage-maintenance.ready
[[ "$acknowledgement" == ready ]]
rm -f /tmp/storage-maintenance.ready
kill -0 "$(cat "$pidfile")"
REMOTE

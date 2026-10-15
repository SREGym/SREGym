#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-hotel-reservation}
ARTIFACT_DIR=${ARTIFACT_DIR:-$(cd -- "$(dirname -- "$0")" && pwd)}
NAMESPACE="$NAMESPACE" bash "$ARTIFACT_DIR/mitigate.sh"

# Recreate the utility container to remove orphaned shell/timer zombies and
# its ephemeral working files, without changing its Deployment or PVC.
kubectl -n "$NAMESPACE" delete pod -l io.kompose.service=mongodb-user --wait=true
kubectl -n "$NAMESPACE" rollout status deployment/mongodb-user --timeout=180s

if [[ -f "$ARTIFACT_DIR/state/profile-cache.json" ]]; then
  client_pod=$(kubectl -n "$NAMESPACE" get pods -l io.kompose.service=frontend -o jsonpath='{.items[0].metadata.name}')
  kubectl -n "$NAMESPACE" exec -i "$client_pod" -- python3 -c '
import base64,json,socket,sys,time
records=json.load(sys.stdin)
with socket.create_connection(("memcached-profile",11211),10) as s:
    f=s.makefile("rb")
    s.sendall(b"flush_all\r\n")
    assert f.readline()==b"OK\r\n"
    for record in records:
        expiry=record["expiration"]
        if expiry>0 and expiry<=time.time(): continue
        if expiry<0: expiry=0
        value=base64.b64decode(record["value"])
        header="set %s %d %d %d\r\n" % (record["key"],record["flags"],expiry,len(value))
        s.sendall(header.encode()+value+b"\r\n")
        assert f.readline()==b"STORED\r\n"
print("Original profile cache restored.")
' < "$ARTIFACT_DIR/state/profile-cache.json"
fi

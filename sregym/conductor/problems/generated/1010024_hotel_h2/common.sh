#!/usr/bin/env bash
set -euo pipefail
: "${NAMESPACE:=hotel-reservation}"
ARTIFACT_DIR="${ARTIFACT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}"
uid=$(kubectl -n "$NAMESPACE" get deploy mongodb-profile -o jsonpath='{.metadata.uid}')
STATE_DIR="$ARTIFACT_DIR/state/$uid"
mongo_eval() { kubectl -n "$NAMESPACE" exec -i deploy/mongodb-profile -- mongo profile-db --quiet --eval "$1"; }
clear_cache() {
 kubectl -n "$NAMESPACE" exec -i deploy/frontend -- python3 - <<'PY'
import socket
s=socket.create_connection(('memcached-profile',11211),10)
s.sendall(b'flush_all\r\n')
assert s.recv(100).startswith(b'OK')
s.close()
PY
}

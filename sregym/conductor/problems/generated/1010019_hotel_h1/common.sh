#!/usr/bin/env bash
set -euo pipefail
NAMESPACE="${NAMESPACE:-hotel-reservation}"
ARTIFACT_DIR="${ARTIFACT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}"
uid=$(kubectl -n "$NAMESPACE" get deploy mongodb-profile -o jsonpath='{.metadata.uid}')
STATE_DIR="$ARTIFACT_DIR/state-$uid"
mongo_eval() { kubectl -n "$NAMESPACE" exec deploy/mongodb-profile -- mongo profile-db --quiet --eval "$1"; }
clear_cache() {
kubectl -n "$NAMESPACE" exec deploy/profile -- python3 -c 'import socket
s=socket.create_connection(("memcached-profile",11211));s.sendall(b"flush_all\r\n");print(s.recv(1024).decode());s.close()'
}

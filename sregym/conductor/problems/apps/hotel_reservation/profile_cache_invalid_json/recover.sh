#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-hotel-reservation}
ARTIFACT_DIR=${ARTIFACT_DIR:-$(cd -- "$(dirname -- "$0")" && pwd)}
export NAMESPACE ARTIFACT_DIR
bash "$ARTIFACT_DIR/mitigate.sh"

# Cleanup remains safe when the reference mitigation has already stopped the writer.
kubectl -n "$NAMESPACE" exec deployment/mongodb-reservation -- bash -c '
if [[ -f /tmp/catalog-refresh.pid ]]; then
  read -r pid < /tmp/catalog-refresh.pid
  if [[ -r /proc/$pid/cmdline ]] && tr "\0" " " < /proc/$pid/cmdline | awk "/catalog-refresh.sh/ {found=1} END {exit !found}"; then
    kill -TERM "$pid"
  fi
fi
rm -f /tmp/catalog-refresh.sh /tmp/catalog-refresh.pid /tmp/catalog-refresh.log
'
owner=$(kubectl -n "$NAMESPACE" get deployment mongodb-reservation -o jsonpath='{.metadata.uid}')
if [[ -f "$ARTIFACT_DIR/profile-cache.json" && -f "$ARTIFACT_DIR/state-owner" && "$(cat "$ARTIFACT_DIR/state-owner")" == "$owner" ]]; then
  kubectl -n "$NAMESPACE" exec -i deployment/profile -- python3 -c '
import socket,sys,json,base64
items=json.load(sys.stdin)
s=socket.create_connection(("memcached-profile",11211),10)
f=s.makefile("rb")
for key in map(str,range(1,81)):
    s.sendall(("delete "+key+"\r\n").encode())
    assert f.readline() in (b"DELETED\r\n",b"NOT_FOUND\r\n")
for key,item in items.items():
    value=base64.b64decode(item["value"])
    s.sendall(("set %s %d 0 %d\r\n"%(key,item["flags"],len(value))).encode()+value+b"\r\n")
    assert f.readline()==b"STORED\r\n"
s.close()
' < "$ARTIFACT_DIR/profile-cache.json"
fi
echo 'Original catalog cache contents restored.'

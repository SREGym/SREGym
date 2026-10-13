#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-hotel-reservation}

# Find and stop the operational publisher from live process state.
mapfile -t mongo_pods < <(kubectl get pods -n "$NAMESPACE" -o json | python3 -c '
import json,sys
for p in json.load(sys.stdin)["items"]:
    if p["metadata"]["labels"].get("io.kompose.service", "").startswith("mongodb-") and p["status"].get("phase")=="Running":
        print(p["metadata"]["name"])
')
for pod in "${mongo_pods[@]}"; do
  kubectl exec -i -n "$NAMESPACE" "$pod" -- bash -s <<'REMOTE'
set -euo pipefail
for entry in /proc/[0-9]*/cmdline; do
  [[ -r "$entry" ]] || continue
  if tr '\0' ' ' < "$entry" 2>/dev/null | awk '$0 ~ /mongo/ && $0 ~ /storage-maintenance[.]js/ {found=1} END {exit !found}'; then
    pid=${entry#/proc/}
    pid=${pid%/cmdline}
    [[ "$pid" != 1 ]] && kill "$pid" 2>/dev/null || true
  fi
done
rm -f /tmp/storage-maintenance.js /tmp/storage-maintenance.pid /tmp/storage-maintenance.log /tmp/storage-maintenance.ready
REMOTE
done

# Restore the standard percentage thresholds, leaving the cache size intact.
kubectl exec -n "$NAMESPACE" deploy/mongodb-profile -- mongo --quiet --eval '
  assert.commandWorked(db.adminCommand({setParameter:1,wiredTigerEngineRuntimeConfig:"eviction_target=80,eviction_trigger=95"}));
  assert.commandWorked(db.adminCommand({setParameter:1,wiredTigerEngineRuntimeConfig:""}));
  assert.commandWorked(db.getSiblingDB("profile-db").runCommand({find:"hotels",filter:{id:"1"},limit:1,maxTimeMS:10000}));
'

# Failed profile lookups may have populated incomplete cached profiles.
kubectl exec -n "$NAMESPACE" deploy/frontend -- python3 -c '
import socket
with socket.create_connection(("memcached-profile",11211),timeout=10) as connection:
    connection.sendall(b"flush_all\r\n")
    assert connection.recv(256).strip()==b"OK"
'

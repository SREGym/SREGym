#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-hotel-reservation}

# Find the unmanaged publisher by inspecting the live process table. Neither
# the saved cache snapshot nor a saved PID is needed for the reference repair.
for pod in $(kubectl -n "$NAMESPACE" get pods -l io.kompose.service=mongodb-user -o jsonpath='{.items[*].metadata.name}'); do
  kubectl -n "$NAMESPACE" exec -i "$pod" -- bash -s <<'SH'
for entry in /proc/[0-9]*/cmdline; do
  [[ -r "$entry" ]] || continue
  command=$(tr '\0' ' ' < "$entry" 2>/dev/null) || continue
  case "$command" in
    'bash /usr/local/bin/catalog-refresh '*)
      pid=${entry#/proc/}; pid=${pid%/cmdline}
      # Stop publication before clearing the cache, including an in-flight
      # iteration. The only children are a brief catalog read or an idle timer.
      kill -STOP "$pid" 2>/dev/null || true
      children=$(cat "/proc/$pid/task/$pid/children" 2>/dev/null) || children=''
      for child in $children; do kill -KILL "$child" 2>/dev/null || true; done
      kill -KILL "$pid" 2>/dev/null || true
      ;;
  esac
done
rm -f /usr/local/bin/catalog-refresh /tmp/catalog-refresh.pid /tmp/catalog-refresh.log
SH
done

catalog_pod=$(kubectl -n "$NAMESPACE" get pods -l io.kompose.service=mongodb-profile -o jsonpath='{.items[0].metadata.name}')
catalog_ids=$(kubectl -n "$NAMESPACE" exec "$catalog_pod" -- mongo --quiet --eval 'print(JSON.stringify(db.getSiblingDB("profile-db").hotels.distinct("id")));')
printf '%s\n' "$catalog_ids" | kubectl -n "$NAMESPACE" exec -i deployment/profile -- python3 -c '
import json, socket, sys
keys=json.load(sys.stdin)
with socket.create_connection(("memcached-profile",11211),10) as s:
    f=s.makefile("rb")
    s.sendall(b"flush_all\r\n")
    assert f.readline()==b"OK\r\n"
    # Explicit invalidation also removes recently accessed LRU entries that can
    # survive an immediate flush in the current one-second cache clock tick.
    for key in keys:
        s.sendall(("delete "+key+"\r\n").encode())
        assert f.readline() in (b"DELETED\r\n",b"NOT_FOUND\r\n")
print("Profile keys invalidated; complete records will be loaded from MongoDB.")
'

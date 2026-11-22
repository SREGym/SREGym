#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-social-network}

# The administrative session's remote address identifies the existing media
# datastore as the snapshot coordinator. Stop it before releasing its lock.
pods=$(kubectl -n "$NAMESPACE" get pods -l service=media-mongodb -o json | python3 -c 'import json,sys; print("\n".join(p["metadata"]["name"] for p in json.load(sys.stdin)["items"] if p["status"].get("phase")=="Running" and not p["metadata"].get("deletionTimestamp")))')
while IFS= read -r pod; do
  [[ -n "$pod" ]] || continue
  kubectl -n "$NAMESPACE" exec "$pod" -- bash -c '
    for f in /proc/[0-9]*/cmdline; do
      [[ -r "$f" ]] || continue
      cmd=$(tr "\0" " " < "$f" 2>/dev/null) || continue
      if [[ "$cmd" == mongo\ * && "$cmd" == *"/var/tmp/mongo-snapshot.js"* ]]; then
        pid=${f#/proc/}; pid=${pid%/cmdline}
        kill -KILL "$pid" 2>/dev/null || true
      fi
    done
    rm -f /var/tmp/mongo-snapshot.js /var/tmp/mongo-snapshot.pid /var/tmp/mongo-snapshot.log /var/tmp/mongo-snapshot.ready
  '
done <<< "$pods"

kubectl -n "$NAMESPACE" exec deploy/post-storage-mongodb -- mongo --quiet --eval '
  while (db.currentOp().fsyncLock) {
    var r=db.adminCommand({fsyncUnlock:1});
    if (!r.ok) { printjson(r); quit(1); }
  }
  if (db.currentOp().fsyncLock) quit(1);
  print("Content storage writes are enabled");
'

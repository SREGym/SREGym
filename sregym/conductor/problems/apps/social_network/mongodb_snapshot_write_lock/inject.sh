#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-social-network}
ARTIFACT_DIR=${ARTIFACT_DIR:-$(cd -- "$(dirname -- "$0")" && pwd)}
mkdir -p "$ARTIFACT_DIR/state"

pod=$(kubectl -n "$NAMESPACE" get pods -l service=media-mongodb -o json | python3 -c 'import json,sys; p=[p for p in json.load(sys.stdin)["items"] if p["status"].get("phase")=="Running" and not p["metadata"].get("deletionTimestamp")]; assert p,"No running media datastore"; print(sorted(p,key=lambda x:x["metadata"]["name"])[0]["metadata"]["name"])')

# The only durable application change is an administrative write freeze. Keep
# the pre-existing lock state for cleanup, without relying on it for mitigation.
if [[ ! -f "$ARTIFACT_DIR/state/original.json" ]]; then
  kubectl -n "$NAMESPACE" exec "$pod" -- bash -c 'for f in /var/tmp/mongo-snapshot.js /var/tmp/mongo-snapshot.pid /var/tmp/mongo-snapshot.log /var/tmp/mongo-snapshot.ready; do [[ ! -e "$f" ]] || { echo "Existing storage maintenance file: $f" >&2; exit 1; }; done'
  kubectl -n "$NAMESPACE" exec deploy/post-storage-mongodb -- mongo --quiet --eval 'print(JSON.stringify({locked:!!db.currentOp().fsyncLock}));' > "$ARTIFACT_DIR/state/original.json.tmp"
  python3 -c 'import json,sys; s=json.load(open(sys.argv[1])); assert not s["locked"],"The post datastore is already write-frozen"' "$ARTIFACT_DIR/state/original.json.tmp"
  mv "$ARTIFACT_DIR/state/original.json.tmp" "$ARTIFACT_DIR/state/original.json"
fi

kubectl -n "$NAMESPACE" exec -i "$pod" -- bash -s <<'REMOTE'
set -euo pipefail
script=/var/tmp/mongo-snapshot.js
pidfile=/var/tmp/mongo-snapshot.pid
log=/var/tmp/mongo-snapshot.log
ready=/var/tmp/mongo-snapshot.ready

if [[ -r "$pidfile" ]]; then
  IFS= read -r pid < "$pidfile"
  if [[ "$pid" =~ ^[0-9]+$ && -r "/proc/$pid/cmdline" ]]; then
    cmd=$(tr '\0' ' ' < "/proc/$pid/cmdline")
    if [[ "$cmd" == *"mongo --quiet --nodb $script"* ]]; then
      # Re-running the operation neither duplicates the coordinator nor nests
      # locks. It does restore the requested write freeze immediately.
      mongo --quiet --host post-storage-mongodb --eval 'if (!db.currentOp().fsyncLock) { var r=db.adminCommand({fsync:1,lock:true}); if (!r.ok) quit(1); }'
      exit 0
    fi
  fi
fi

cat > "$script" <<'JAVASCRIPT'
// Coordinate the post-store quiescence required by content snapshots.
// The transfer is run separately; retain the freeze until it has committed.
(function () {
    var host = "post-storage-mongodb:27017";
    var admin = new Mongo(host).getDB("admin");
    function freeze() {
        if (!admin.currentOp().fsyncLock) {
            var reply = admin.runCommand({fsync: 1, lock: true});
            if (!reply.ok) throw new Error("Unable to quiesce content storage");
        }
    }
    freeze();
    if (run("bash", "-c", "printf 'ready\\n' > /var/tmp/mongo-snapshot.ready") !== 0)
        throw new Error("Snapshot coordinator startup was not acknowledged");
    var retryAt = null;
    for (;;) {
        try {
            if (admin.currentOp().fsyncLock) {
                retryAt = null;
            } else {
                // Allow a previous transfer to drain before reacquiring.
                if (retryAt === null) retryAt = Date.now() + 210000;
                if (Date.now() >= retryAt) {
                    freeze();
                    retryAt = null;
                }
            }
        } catch (e) {
            // Reopen the administrative session after a datastore restart.
            try { admin = new Mongo(host).getDB("admin"); } catch (ignored) {}
        }
        sleep(5000);
    }
})();
JAVASCRIPT

rm -f "$ready"
mkfifo -m 600 "$ready"
exec 3<> "$ready"
nohup mongo --quiet --nodb "$script" > "$log" 2>&1 < /dev/null 3>&- &
pid=$!
printf '%s\n' "$pid" > "$pidfile"
# A FIFO acknowledges the first completed database operation. There is no
# race-dependent startup sleep, and no temporary Kubernetes object.
status=''
if ! IFS= read -r -t 45 status <&3 || [[ "$status" != ready ]]; then
  kill -KILL "$pid" 2>/dev/null || true
  cat "$log" >&2
  rm -f "$ready"
  exit 1
fi
exec 3>&-
rm -f "$ready"
kill -0 "$pid"
REMOTE

kubectl -n "$NAMESPACE" exec deploy/post-storage-mongodb -- mongo --quiet --eval 'if (!db.currentOp().fsyncLock) quit(1); print("Content storage is quiesced");'

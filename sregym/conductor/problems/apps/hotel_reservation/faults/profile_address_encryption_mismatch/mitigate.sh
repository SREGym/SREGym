#!/usr/bin/env bash
set -euo pipefail
NAMESPACE="${NAMESPACE:-hotel-reservation}"
invalidate_profile_cache() {
  local PROFILE_IDS
  PROFILE_IDS="$(kubectl -n "$NAMESPACE" exec deploy/mongodb-profile -- mongo --quiet profile-db --eval 'print(JSON.stringify(db.hotels.find({}, {_id:0,id:1}).toArray().map(function(d){return d.id;})))')"
  kubectl -n "$NAMESPACE" exec -i deploy/profile -- python3 - "$PROFILE_IDS" <<'PY'
import socket,json,sys
ids=json.loads(sys.argv[1])
with socket.create_connection(('memcached-profile',11211),10) as s:
    s.settimeout(10)
    with s.makefile('rb') as reader:
        for ident in ids:
            s.sendall(('delete %s\r\n' % ident).encode())
            if reader.readline() not in (b'DELETED\r\n',b'NOT_FOUND\r\n'):
                raise RuntimeError('Cache invalidation was not acknowledged')
PY
}
# The publisher and its key are discovered in running containers, not in an
# artifact-side backup. Stop all copies before restoring the live representation.
PODS="$(kubectl -n "$NAMESPACE" get pods -l io.kompose.service=user -o jsonpath='{.items[*].metadata.name}')"
RESTORED=0
for POD in $PODS; do
  kubectl -n "$NAMESPACE" exec -i "$POD" -- python3 - <<'PY'
import os, signal, select
for ident in os.listdir('/proc'):
    if not ident.isdigit():
        continue
    try:
        argv = open('/proc/'+ident+'/cmdline','rb').read().split(b'\0')
        if b'/tmp/contact-sync.py' in argv and b'run' in argv:
            fd = os.pidfd_open(int(ident))
            os.kill(int(ident),signal.SIGTERM)
            if not select.select([fd],[],[],30)[0]:
                os.kill(int(ident),signal.SIGKILL)
                if not select.select([fd],[],[],10)[0]:
                    raise RuntimeError('Contact publisher did not exit')
            os.close(fd)
    except (FileNotFoundError,ProcessLookupError):
        pass
PY
  if kubectl -n "$NAMESPACE" exec "$POD" -- sh -c '[ -f /tmp/contact-sync.py ] && [ -f /tmp/contact-sync.key ]' 2>/dev/null; then
    kubectl -n "$NAMESPACE" exec "$POD" -- python3 /tmp/contact-sync.py restore
    RESTORED=1
  fi
done
if [[ "$RESTORED" == 0 ]]; then
  ENCRYPTED_COUNT="$(kubectl -n "$NAMESPACE" exec deploy/mongodb-profile -- mongo --quiet profile-db --eval 'print(db.hotels.count({address:{$type:"binData"}}))')"
  if [[ "$ENCRYPTED_COUNT" == 0 ]]; then
    invalidate_profile_cache
    printf '%s\n' 'Live catalog already uses the reader-compatible representation.'
    exit 0
  fi
  # The stock hotel's reference catalog can be regenerated from the application
  # itself if the abandoned publisher's temporary key was lost in a pod restart.
  kubectl -n "$NAMESPACE" exec deploy/mongodb-profile -- mongo --quiet profile-db --eval '
    var r=db.hotels.remove({address:{$type:"binData"}}); printjson(r); if(r.hasWriteError()) quit(1);'
  PROFILE_POD="$(kubectl -n "$NAMESPACE" get pods -l io.kompose.service=profile -o jsonpath='{.items[0].metadata.name}')"
  RESTARTS="$(kubectl -n "$NAMESPACE" get pod "$PROFILE_POD" -o jsonpath='{.status.containerStatuses[0].restartCount}')"
  # Restart the process in its current pod, retaining its advertised pod address.
  # The stock startup populates absent catalog records from cmd/profile/db.go.
  kubectl -n "$NAMESPACE" exec "$PROFILE_POD" -- sh -c 'kill -TERM 1' || true
  kubectl -n "$NAMESPACE" wait --for="jsonpath={.status.containerStatuses[0].restartCount}=$((RESTARTS+1))" "pod/$PROFILE_POD" --timeout=180s
  kubectl -n "$NAMESPACE" wait --for=condition=Ready "pod/$PROFILE_POD" --timeout=180s
  kubectl -n "$NAMESPACE" exec deploy/mongodb-profile -- mongo --quiet profile-db --eval '
    if(db.hotels.count({address:{$type:"object"}}) < 80) quit(1); print("Reference catalog available");'
  invalidate_profile_cache
fi
printf '%s\n' 'Contact publisher stopped; live catalog and profile cache restored.'

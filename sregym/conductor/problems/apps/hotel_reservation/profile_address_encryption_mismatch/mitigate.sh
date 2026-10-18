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
# Stop all copies before restoring the live representation.
PODS="$(kubectl -n "$NAMESPACE" get pods -l io.kompose.service=user -o jsonpath='{.items[*].metadata.name}')"
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
done
if [[ "${1:-}" == "--stop-only" ]]; then
  exit 0
fi
ENCRYPTED_COUNT="$(kubectl -n "$NAMESPACE" exec deploy/mongodb-profile -- mongo --quiet profile-db --eval 'print(db.hotels.count({address:{$type:"binData"}}))')"
if [[ "$ENCRYPTED_COUNT" == 0 ]]; then
  invalidate_profile_cache
  printf '%s\n' 'Live catalog already uses the reader-compatible representation.'
  exit 0
fi
# Both operational resources survive replacement of the publisher pod. A
# missing or invalid key fails before any catalog record is changed.
kubectl -n "$NAMESPACE" get configmap contact-export -o jsonpath='{.data.contact-sync\.py}' |
  kubectl -n "$NAMESPACE" exec -i deploy/profile -- sh -c 'cat > /tmp/contact-sync.py'
kubectl -n "$NAMESPACE" get secret contact-export-key -o jsonpath='{.data.key}' |
  kubectl -n "$NAMESPACE" exec -i deploy/profile -- python3 -c 'import base64,os,sys; key=base64.b64decode(sys.stdin.read(),validate=True); assert len(key)==32; fd=os.open("/tmp/contact-sync.key",os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600); os.write(fd,key); os.close(fd)'
kubectl -n "$NAMESPACE" exec deploy/profile -- python3 /tmp/contact-sync.py restore
kubectl -n "$NAMESPACE" exec deploy/profile -- sh -c 'rm -f /tmp/contact-sync.py /tmp/contact-sync.key'
printf '%s\n' 'Contact publisher stopped; live catalog and profile cache restored.'

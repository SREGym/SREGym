#!/usr/bin/env bash
set -euo pipefail
NAMESPACE="${NAMESPACE:-hotel-reservation}"
ARTIFACT_DIR="${ARTIFACT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}"
cd "$ARTIFACT_DIR"
POD="$(kubectl -n "$NAMESPACE" get pods -l io.kompose.service=user -o jsonpath='{.items[0].metadata.name}')"
STORE_UID="$(kubectl -n "$NAMESPACE" get pvc profile-pvc -o jsonpath='{.metadata.uid}')"
kubectl -n "$NAMESPACE" exec -i "$POD" -- sh -c 'cat > /tmp/contact-sync.py' < contact_sync.py
if [[ ! -s original.bson64 || ! -s store.uid || "$(cat store.uid)" != "$STORE_UID" ]]; then
  kubectl -n "$NAMESPACE" exec "$POD" -- python3 /tmp/contact-sync.py snapshot > original.bson64
  printf '%s\n' "$STORE_UID" > store.uid
fi
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
kubectl -n "$NAMESPACE" exec "$POD" -- python3 /tmp/contact-sync.py migrate
kubectl -n "$NAMESPACE" exec -i "$POD" -- python3 - <<'PY'
import os, subprocess
proc = subprocess.Popen(['python3','/tmp/contact-sync.py','run'],
                        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,start_new_session=True,close_fds=True)
with open('/tmp/contact-sync.pid','w') as f:
    f.write(str(proc.pid))
print('Contact publication worker running: %s' % proc.pid)
PY
kubectl -n "$NAMESPACE" exec "$POD" -- python3 /tmp/contact-sync.py status

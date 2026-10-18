#!/usr/bin/env bash
set -euo pipefail
NAMESPACE="${NAMESPACE:-hotel-reservation}"
ARTIFACT_DIR="${ARTIFACT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}"
cd "$ARTIFACT_DIR"
POD="$(kubectl -n "$NAMESPACE" get pods -l io.kompose.service=user -o jsonpath='{.items[0].metadata.name}')"
STORE_UID="$(kubectl -n "$NAMESPACE" get pvc profile-pvc -o jsonpath='{.metadata.uid}')"
python3 - "$NAMESPACE" "$STORE_UID" "$ARTIFACT_DIR/contact_sync.py" <<'PY'
import base64,json,os,pathlib,subprocess,sys
namespace,owner,source=sys.argv[1:]
owner_ref={'apiVersion':'v1','kind':'PersistentVolumeClaim','name':'profile-pvc','uid':owner}
objects=[('secret','contact-export-key',{'key':base64.b64encode(os.urandom(32)).decode()}),
         ('configmap','contact-export',{'contact-sync.py':pathlib.Path(source).read_text()})]
for resource,name,data in objects:
    result=subprocess.run(['kubectl','-n',namespace,'get',resource,name,'--ignore-not-found','-o','json'],
                          capture_output=True,text=True,check=True)
    if result.stdout.strip():
        current=json.loads(result.stdout)
        if owner_ref not in current['metadata'].get('ownerReferences',[]):
            raise RuntimeError('Existing contact-export resource belongs to another catalog')
        if resource=='secret' and len(base64.b64decode(current['data']['key'],validate=True))!=32:
            raise RuntimeError('Contact export key must contain 32 bytes')
        if resource=='configmap' and current.get('data')!=data:
            raise RuntimeError('Contact export code differs from the active catalog version')
        continue
    obj={'apiVersion':'v1','kind':'Secret' if resource=='secret' else 'ConfigMap',
         'metadata':{'name':name,'namespace':namespace,'ownerReferences':[owner_ref]},'data':data}
    subprocess.run(['kubectl','create','-f','-'],input=json.dumps(obj),text=True,check=True)
PY
kubectl -n "$NAMESPACE" exec -i "$POD" -- sh -c 'cat > /tmp/contact-sync.py' < contact_sync.py
kubectl -n "$NAMESPACE" get secret contact-export-key -o jsonpath='{.data.key}' |
  kubectl -n "$NAMESPACE" exec -i "$POD" -- python3 -c 'import base64,os,sys; key=base64.b64decode(sys.stdin.read(),validate=True); assert len(key)==32; fd=os.open("/tmp/contact-sync.key",os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600); os.write(fd,key); os.close(fd)'
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

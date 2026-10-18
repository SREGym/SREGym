#!/usr/bin/env bash
set -euo pipefail
NAMESPACE="${NAMESPACE:-hotel-reservation}"
ARTIFACT_DIR="${ARTIFACT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}"
cd "$ARTIFACT_DIR"
bash "$ARTIFACT_DIR/mitigate.sh" --stop-only
POD="$(kubectl -n "$NAMESPACE" get pods -l io.kompose.service=user -o jsonpath='{.items[0].metadata.name}')"
STORE_UID="$(kubectl -n "$NAMESPACE" get pvc profile-pvc -o jsonpath='{.metadata.uid}')"
if [[ -s original.bson64 && -s store.uid && "$(cat store.uid)" == "$STORE_UID" ]]; then
  kubectl -n "$NAMESPACE" exec -i "$POD" -- sh -c 'cat > /tmp/contact-sync.py' < contact_sync.py
  kubectl -n "$NAMESPACE" exec -i "$POD" -- python3 /tmp/contact-sync.py restore-snapshot < original.bson64
else
  bash "$ARTIFACT_DIR/mitigate.sh"
fi
for POD in $(kubectl -n "$NAMESPACE" get pods -l io.kompose.service=user -o jsonpath='{.items[*].metadata.name}'); do
  kubectl -n "$NAMESPACE" exec "$POD" -- sh -c 'rm -f /tmp/contact-sync.py /tmp/contact-sync.key /tmp/contact-sync.pid /tmp/__pycache__/contact-sync.cpython-*.pyc; rmdir /tmp/__pycache__ 2>/dev/null || true'
done
python3 - "$NAMESPACE" "$STORE_UID" <<'PY'
import json,subprocess,sys
namespace,owner=sys.argv[1:]
for resource,name in [('configmap','contact-export'),('secret','contact-export-key')]:
    result=subprocess.run(['kubectl','-n',namespace,'get',resource,name,'--ignore-not-found','-o','json'],
                          capture_output=True,text=True,check=True)
    if result.stdout.strip():
        obj=json.loads(result.stdout)
        if any(ref.get('uid')==owner for ref in obj['metadata'].get('ownerReferences',[])):
            subprocess.run(['kubectl','-n',namespace,'delete',resource,name],check=True)
PY
printf '%s\n' 'Original catalog representation restored and publication workspace removed.'

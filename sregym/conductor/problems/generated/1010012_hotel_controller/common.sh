#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-hotel-reservation}
ARTIFACT_DIR=${ARTIFACT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}
export NAMESPACE ARTIFACT_DIR
STATE_DIR="$ARTIFACT_DIR/state/$(kubectl -n "$NAMESPACE" get deploy mongodb-profile -o jsonpath='{.metadata.uid}')"
mongo_container() {
  kubectl -n "$NAMESPACE" get deploy mongodb-profile -o json | python3 -c 'import json,sys; d=json.load(sys.stdin); print(next(c["name"] for c in d["spec"]["template"]["spec"]["containers"] if any(p["containerPort"]==27017 for p in c.get("ports",[]))))'
}
mongo_eval() {
  kubectl -n "$NAMESPACE" exec deploy/mongodb-profile -c "$(mongo_container)" -- mongo profile-db --quiet --eval "$1"
}
clear_profile_cache() {
  kubectl -n "$NAMESPACE" exec deploy/profile -- python3 -c 'import socket; s=socket.create_connection(("memcached-profile",11211),10); s.sendall(b"flush_all\r\n"); r=s.recv(128); s.close(); assert r.startswith(b"OK"),r'
}
wait_for_mongo() {
  # The original Deployment has no readiness probe. Wait for a real ping,
  # bounded by elapsed time and command timeouts rather than a startup delay.
  python3 - "$NAMESPACE" "$(mongo_container)" <<'PY'
import subprocess, sys, time
end = time.monotonic() + 180
command = ['kubectl','-n',sys.argv[1],'exec','deploy/mongodb-profile','-c',sys.argv[2],
           '--','mongo','profile-db','--quiet','--eval','if(!db.runCommand({ping:1}).ok) quit(1);']
while time.monotonic() < end:
    try:
        result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        if result.returncode == 0:
            sys.exit(0)
    except subprocess.TimeoutExpired:
        pass
sys.exit('MongoDB did not become available within 180 seconds')
PY
}

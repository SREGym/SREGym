#!/usr/bin/env bash
set -euo pipefail
NAMESPACE="${NAMESPACE:-hotel-reservation}"

running_user_pods() {
  kubectl -n "$NAMESPACE" get pods -l io.kompose.service=user --field-selector=status.phase=Running -o jsonpath='{.items[*].metadata.name}'
}

stop_publishers() {
  local PODS POD
  PODS="$(running_user_pods)"
  for POD in $PODS; do
    kubectl -n "$NAMESPACE" exec -i "$POD" -- python3 - <<'PY'
import os, signal, select

me = os.getpid()
for ident in os.listdir('/proc'):
    if not ident.isdigit() or int(ident) == me:
        continue
    try:
        argv = open('/proc/' + ident + '/cmdline', 'rb').read().split(b'\0')
        if any(b'contact-sync.py' in arg or b'contact_sync.py' in arg for arg in argv) and b'run' in argv:
            fd = os.pidfd_open(int(ident))
            os.kill(int(ident), signal.SIGTERM)
            if not select.select([fd], [], [], 30)[0]:
                os.kill(int(ident), signal.SIGKILL)
                if not select.select([fd], [], [], 10)[0]:
                    raise RuntimeError('Contact publisher did not exit')
            os.close(fd)
    except (FileNotFoundError, ProcessLookupError):
        pass
try:
    os.remove('/tmp/contact-sync.pid')
except FileNotFoundError:
    pass
PY
  done
  # Confirm no publisher copy remains alive in any running user pod.
  PODS="$(running_user_pods)"
  for POD in $PODS; do
    kubectl -n "$NAMESPACE" exec -i "$POD" -- python3 - <<'PY'
import os

me = os.getpid()
alive = []
for ident in os.listdir('/proc'):
    if not ident.isdigit() or int(ident) == me:
        continue
    try:
        argv = open('/proc/' + ident + '/cmdline', 'rb').read().split(b'\0')
        if any(b'contact-sync.py' in arg or b'contact_sync.py' in arg for arg in argv) and b'run' in argv:
            alive.append((ident, [a.decode(errors='replace') for a in argv if a]))
    except (FileNotFoundError, ProcessLookupError):
        pass
if alive:
    raise RuntimeError(f'Contact publisher still running: {alive}')
PY
  done
}

invalidate_profile_cache() {
  local PROFILE_IDS
  PROFILE_IDS="$(kubectl -n "$NAMESPACE" exec deploy/mongodb-profile -- mongo --quiet profile-db --eval 'print(JSON.stringify(db.hotels.find({}, {_id:0,id:1}).toArray().map(function(d){return d.id;})))')"
  kubectl -n "$NAMESPACE" exec -i deploy/profile -- python3 - "$PROFILE_IDS" <<'PY'
import socket, json, sys

ids = json.loads(sys.argv[1])
with socket.create_connection(('memcached-profile', 11211), 10) as s:
    s.settimeout(10)
    with s.makefile('rb') as reader:
        for ident in ids:
            s.sendall(('delete %s\r\n' % ident).encode())
            if reader.readline() not in (b'DELETED\r\n', b'NOT_FOUND\r\n'):
                raise RuntimeError('Cache invalidation was not acknowledged')
PY
}

wait_for_fixed_responses() {
  local deadline=$((SECONDS + 60))
  local probe_err=""
  while true; do
    if probe_err="$(kubectl -n "$NAMESPACE" exec -i deploy/profile -- python3 - <<'PY' 2>&1
import json, urllib.request

probes = [
    '/hotels?lat=37.7749&lon=-122.4194&inDate=2030-01-15&outDate=2030-01-16',
    '/recommendations?lat=37.7749&lon=-122.4194&require=price',
    '/recommendations?lat=37.7749&lon=-122.4194&require=dis',
    '/recommendations?lat=37.7749&lon=-122.4194&require=rate',
]
for path in probes:
    with urllib.request.urlopen('http://frontend:5000' + path, timeout=10) as resp:
        if resp.status != 200:
            raise RuntimeError(f'{path} returned HTTP {resp.status}')
        body = json.loads(resp.read().decode())
    if not isinstance(body, dict) or body.get('type') != 'FeatureCollection' or not body.get('features'):
        raise RuntimeError(f'{path} returned no hotel features')
    for feature in body['features']:
        props = feature.get('properties') or {}
        coords = (feature.get('geometry') or {}).get('coordinates') or []
        if not feature.get('id') or not props.get('name') or not props.get('phone_number'):
            raise RuntimeError(f'{path} returned incomplete hotel profile')
        if (
            len(coords) != 2
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) or abs(v) <= 0.001 for v in coords)
        ):
            raise RuntimeError(f'{path} returned invalid hotel coordinates: {coords}')
PY
    )"; then
      return 0
    fi
    if (( SECONDS >= deadline )); then
      printf '%s\n%s\n' 'Frontend /hotels and /recommendations did not serve fixed profiles in time.' "$probe_err" >&2
      return 1
    fi
    invalidate_profile_cache
    sleep 2
  done
}

verify_and_wait_fixed() {
  local remaining
  remaining="$(kubectl -n "$NAMESPACE" exec deploy/mongodb-profile -- mongo --quiet profile-db --eval 'print(db.hotels.count({address:{$type:"binData"}}))' | tr -d '[:space:]')"
  if [[ "$remaining" != "0" ]]; then
    printf 'Encrypted hotel addresses still present in profile-db: %s\n' "$remaining" >&2
    return 1
  fi
  # Clear memcached after the database restore settles so any concurrent
  # reader from before the write cannot leave an empty-address cache entry.
  sleep 1
  invalidate_profile_cache
  wait_for_fixed_responses
}

if [[ "${1:-}" == "--verify-only" ]]; then
  verify_and_wait_fixed
  exit 0
fi

# Stop all copies before restoring the live representation.
stop_publishers
if [[ "${1:-}" == "--stop-only" ]]; then
  exit 0
fi

ENCRYPTED_COUNT="$(kubectl -n "$NAMESPACE" exec deploy/mongodb-profile -- mongo --quiet profile-db --eval 'print(db.hotels.count({address:{$type:"binData"}}))' | tr -d '[:space:]')"
if [[ "$ENCRYPTED_COUNT" == 0 ]]; then
  verify_and_wait_fixed
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
verify_and_wait_fixed
printf '%s\n' 'Contact publisher stopped; live catalog and profile cache restored.'

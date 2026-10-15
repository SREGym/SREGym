#!/usr/bin/env bash
set -euo pipefail
NAMESPACE="${NAMESPACE:-social-network}"
ARTIFACT_DIR="${ARTIFACT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}"
export NAMESPACE ARTIFACT_DIR

# Capture the native settings once per deployment, before touching runtime state.
python3 - <<'PY'
import json, os, pathlib, subprocess
ns = os.environ['NAMESPACE']
state = pathlib.Path(os.environ['ARTIFACT_DIR']) / 'original.json'
def k(*a):
    return subprocess.check_output(['kubectl','-n',ns,*a], text=True)
targets = ['user-timeline-redis','home-timeline-redis']
uids = {s: json.loads(k('get','service',s,'-o','json'))['metadata']['uid'] for s in targets}
if state.exists() and json.loads(state.read_text()).get('uids') == uids:
    raise SystemExit(0)
limits = {}
for s in targets:
    lines = k('exec','deployment/'+s,'--','redis-cli','--raw','CONFIG','GET','client-output-buffer-limit').splitlines()
    if len(lines) != 2 or lines[0] != 'client-output-buffer-limit':
        raise RuntimeError('Cannot read existing Redis session limits')
    limits[s] = lines[1]
state.write_text(json.dumps({'uids':uids,'limits':limits}, indent=2)+'\n')
PY

source_pod="$(kubectl -n "$NAMESPACE" get pods -l service=media-frontend --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}')"
[[ -n "$source_pod" ]]
kubectl -n "$NAMESPACE" exec -i "$source_pod" -c media-frontend -- sh -c 'cat > /tmp/cache-session-policy.py' <<'PY'
from __future__ import print_function
import os
import signal
import socket
import subprocess
import sys
import time

# Normal-session budget from the maintenance profile, expressed in MiB.
NORMAL_OUTPUT_MIB = 16
REFRESH_SETTLE_SECONDS = 145
TARGETS = ('user-timeline-redis', 'home-timeline-redis')
PROFILE = 'normal %d 0 0 slave 268435456 67108864 60 pubsub 33554432 8388608 60' % NORMAL_OUTPUT_MIB

def request(host, *args):
    connection = socket.create_connection((host, 6379), 3)
    connection.settimeout(3)
    reader = connection.makefile('rb')
    try:
        wire = '*%d\r\n' % len(args)
        for value in args:
            value = str(value)
            wire += '$%d\r\n%s\r\n' % (len(value), value)
        connection.sendall(wire.encode('ascii'))
        def read():
            line = reader.readline()
            if not line:
                raise IOError('Session ended before reply')
            kind, value = line[:1], line[1:-2]
            if kind == b'+':
                return value.decode('ascii')
            if kind == b'-':
                raise IOError(value.decode('ascii'))
            if kind == b'$':
                length = int(value)
                if length < 0:
                    return None
                body = reader.read(length)
                reader.read(2)
                return body.decode('ascii')
            if kind == b'*':
                return [read() for _ in range(int(value))]
            if kind == b':':
                return int(value)
            raise IOError('Unrecognized reply')
        return read()
    finally:
        reader.close()
        connection.close()

def publish(host):
    if request(host, 'CONFIG', 'SET', 'client-output-buffer-limit', PROFILE) != 'OK':
        raise IOError('Session policy publication did not complete')

def policy_changed(host):
    try:
        response = request(host, 'CONFIG', 'GET', 'client-output-buffer-limit')
        return int(response[1].split()[1]) != NORMAL_OUTPUT_MIB
    except (IOError, OSError, socket.error, ValueError, IndexError):
        return False

def existing():
    own = os.getpid()
    for name in os.listdir('/proc'):
        if not name.isdigit() or int(name) == own:
            continue
        try:
            parts = open('/proc/'+name+'/cmdline', 'rb').read().split(b'\0')
            if b'/tmp/cache-session-policy.py' in parts and b'--run' in parts:
                return int(name)
        except IOError:
            pass
    return None

if sys.argv[1:] == ['--apply']:
    for host in TARGETS:
        publish(host)
elif sys.argv[1:] == ['--running']:
    sys.exit(0 if existing() is not None else 1)
elif sys.argv[1:] == ['--start']:
    if existing() is None:
        with open('/dev/null', 'rb') as incoming, open('/tmp/cache-session-policy.log', 'ab') as outgoing:
            process = subprocess.Popen(
                [sys.executable, '/tmp/cache-session-policy.py', '--run'],
                stdin=incoming, stdout=subprocess.PIPE, stderr=outgoing,
                close_fds=True, preexec_fn=os.setsid)
            if process.stdout.readline() != b'ready\n':
                raise IOError('Session policy owner did not start')
            process.stdout.close()
elif sys.argv[1:] == ['--run']:
    if existing() is not None:
        sys.exit(0)
    deadlines = dict((host, None) for host in TARGETS)
    sys.stdout.write('ready\n')
    sys.stdout.flush()
    with open('/tmp/cache-session-policy.log', 'ab') as output:
        os.dup2(output.fileno(), 1)
    while True:
        now = time.time()
        for host in TARGETS:
            deadline = deadlines[host]
            if deadline is None:
                if policy_changed(host):
                    deadlines[host] = now + REFRESH_SETTLE_SECONDS
            elif now >= deadline:
                try:
                    publish(host)
                    deadlines[host] = None
                except (IOError, OSError, socket.error):
                    pass
        pending = [deadline for deadline in deadlines.values() if deadline is not None]
        delay = min(8, max(0.05, min(pending) - time.time())) if pending else 8
        time.sleep(delay)
else:
    sys.exit(2)
PY

# Apply the full policy synchronously, then detach its existing operational owner.
kubectl -n "$NAMESPACE" exec "$source_pod" -c media-frontend -- python /tmp/cache-session-policy.py --apply
kubectl -n "$NAMESPACE" exec "$source_pod" -c media-frontend -- python /tmp/cache-session-policy.py --start
kubectl -n "$NAMESPACE" exec "$source_pod" -c media-frontend -- python /tmp/cache-session-policy.py --running

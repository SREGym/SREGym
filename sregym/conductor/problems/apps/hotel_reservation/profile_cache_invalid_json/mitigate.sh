#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-hotel-reservation}
export NAMESPACE

# Attribute the unexpected cache writer from live socket and pod state.
python3 - <<'PY'
import json,os,socket,subprocess
ns=os.environ['NAMESPACE']
def k(*args, input=None):
    return subprocess.check_output(['kubectl','-n',ns,*args],input=input,text=True)
pods=json.loads(k('get','pods','-o','json'))['items']
cache_ips={p['status'].get('podIP') for p in pods if p['metadata'].get('labels',{}).get('io.kompose.service')=='memcached-profile'}
cache_ips.add(json.loads(k('get','service','memcached-profile','-o','json'))['spec']['clusterIP'])
def addresses(raw):
    result=[]
    for line in raw.splitlines()[1:]:
        f=line.split()
        if len(f)<10: continue
        def addr(value):
            a,b=value.split(':')
            return socket.inet_ntoa(bytes.fromhex(a)[::-1]),int(b,16)
        result.append((addr(f[1]),addr(f[2]),f[3],f[9]))
    return result
for pod in pods:
    ip=pod['status'].get('podIP')
    if not ip or pod['status'].get('phase')!='Running' or pod['metadata'].get('labels',{}).get('io.kompose.service')=='profile': continue
    name=pod['metadata']['name']
    rows=addresses(k('exec',name,'--','cat','/proc/net/tcp'))
    # CLOSE_WAIT also attributes a sleeping publisher after the cache was restarted.
    inodes={inode for local,remote,state,inode in rows if remote[0] in cache_ips and remote[1]==11211 and inode!='0'}
    if not inodes: continue
    stop=r'''
set -eu
pids=""
for dir in /proc/[0-9]*; do
  pid=${dir##*/}
  [ "$pid" -gt 1 ] || continue
  for fd in "$dir"/fd/*; do
    target=$(readlink "$fd" 2>/dev/null || true)
    for inode in "$@"; do
      if [ "$target" = "socket:[$inode]" ]; then
        pids="$pids $pid"
      fi
    done
  done
done
for pid in $pids; do kill -TERM "$pid" 2>/dev/null || true; done
for file in /tmp/*.sh; do
  [ -f "$file" ] || continue
  if awk '/memcached-profile/ {cache=1} /tojsononeline/ {serializer=1} END {exit !(cache && serializer)}' "$file"; then
    rm -f "$file" "${file%.sh}.pid" "${file%.sh}.log"
  fi
done
'''
    k('exec','-i',name,'--','bash','-s','--',*sorted(inodes),input=stop)
    print('Stopped unmanaged cache writer in '+name)
# Invalidate the live catalog keys explicitly, including any surviving lazy-flush item.
keys=json.loads(k('exec','deployment/mongodb-profile','--','mongo','--quiet','profile-db','--eval','print(JSON.stringify(db.hotels.distinct("id")))'))
code=r'''
import socket,json,sys
s=socket.create_connection(('memcached-profile',11211),10)
f=s.makefile('rb')
for key in json.loads(sys.argv[1]):
    s.sendall(('delete '+key+'\r\n').encode())
    assert f.readline() in (b'DELETED\r\n',b'NOT_FOUND\r\n')
s.close()
'''
k('exec','deployment/profile','--','python3','-c',code,json.dumps(keys))
print('Profile cache invalidated; canonical catalog remains in MongoDB.')
PY

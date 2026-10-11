#!/bin/bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-hotel-reservation}
export NAMESPACE
python3 - <<'PY'
import json,os,subprocess
ns=os.environ['NAMESPACE']
def kget(*args):
 return json.loads(subprocess.check_output(['kubectl','-n',ns,'get',*args,'-o','json']))
pods=kget('pods','-l','io.kompose.service=rate')['items']
addresses={p['status'].get('podIP') for p in pods}
addresses.add(kget('service','rate')['spec']['clusterIP'])
# Classify registrations against live rate endpoints, rather than a remembered ID.
remote='''import sys,json,urllib.request
allowed=set(json.load(sys.stdin))
base="http://consul:8500/v1/"
entries=json.load(urllib.request.urlopen(base+"catalog/service/srv-rate",timeout=20))
for entry in entries:
 address=entry.get("ServiceAddress") or entry["Address"]
 if address in allowed and entry["ServicePort"]==8084:
  continue
 payload={"Datacenter":entry["Datacenter"],"Node":entry["Node"],"ServiceID":entry["ServiceID"]}
 request=urllib.request.Request(base+"catalog/deregister",data=json.dumps(payload).encode(),method="PUT",headers={"Content-Type":"application/json"})
 result=urllib.request.urlopen(request,timeout=20).read()
 assert result.strip()==b"true",result
 # External nodes with no remaining services are obsolete migration records.
 node=json.load(urllib.request.urlopen(base+"catalog/node/"+entry["Node"],timeout=20))
 if node and not node.get("Services"):
  request=urllib.request.Request(base+"catalog/deregister",data=json.dumps({"Datacenter":entry["Datacenter"],"Node":entry["Node"]}).encode(),method="PUT",headers={"Content-Type":"application/json"})
  assert urllib.request.urlopen(request,timeout=20).read().strip()==b"true"
'''
subprocess.run(['kubectl','-n',ns,'exec','-i','deployment/frontend','--','python3','-c',remote],input=json.dumps(sorted(a for a in addresses if a)).encode(),check=True)
PY

#!/bin/bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-hotel-reservation}
export NAMESPACE
python3 - <<'PY'
import json,os,subprocess
ns=os.environ['NAMESPACE']
def get(*args):
 return json.loads(subprocess.check_output(['kubectl','-n',ns,'get',*args,'-o','json']))
address=get('service','user')['spec']['clusterIP']
# A regional registrar published its shared upstream under the rate capability.
payload={'Node':'regional-gateway-01','Address':address,'NodeMeta':{'region':'primary'},
 'Service':{'ID':'rate-regional-01','Service':'srv-rate','Address':address,'Port':8086,'Tags':['regional']},
 'Checks':[{'Node':'regional-gateway-01','CheckID':'service:rate-regional-01','Name':'Regional upstream TCP','Status':'passing','ServiceID':'rate-regional-01','ServiceName':'srv-rate'}]}
remote='import sys,json,urllib.request; data=sys.stdin.buffer.read(); r=urllib.request.Request("http://consul:8500/v1/catalog/register",data=data,method="PUT",headers={"Content-Type":"application/json"}); result=urllib.request.urlopen(r,timeout=20).read(); assert result.strip()==b"true",result'
subprocess.run(['kubectl','-n',ns,'exec','-i','deployment/frontend','--','python3','-c',remote],input=json.dumps(payload).encode(),check=True)
PY

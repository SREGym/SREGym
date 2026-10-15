#!/usr/bin/env bash
set -euo pipefail
NAMESPACE="${NAMESPACE:-hotel-reservation}"
ARTIFACT_DIR="${ARTIFACT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}"
export NAMESPACE ARTIFACT_DIR
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/routing-lib.sh"
stop_endpoint_exports

python3 - <<'PY'
import hashlib, json, os, subprocess
ns = os.environ['NAMESPACE']
path = os.path.join(os.environ['ARTIFACT_DIR'],'routing-state.json')
svc = json.loads(subprocess.check_output(['kubectl','-n',ns,'get','service','frontend','-o','json']))
uid = svc['metadata']['uid']
if os.path.exists(path):
    with open(path) as f:
        state = json.load(f)
    if state.get('namespace') != ns or state.get('service_uid') != uid:
        state = {'name':'frontend-'+hashlib.sha256((uid+'/http').encode()).hexdigest()[:5], 'prior':None}
else:
    state = {'name':'frontend-'+hashlib.sha256((uid+'/http').encode()).hexdigest()[:5], 'prior':None}
if state['prior'] is None:
    subprocess.run(['kubectl','-n',ns,'delete','endpointslice',state['name'],'--ignore-not-found','--wait=true'],check=True)
else:
    obj = state['prior']
    for key in ['resourceVersion','uid','creationTimestamp','managedFields']:
        obj['metadata'].pop(key,None)
    subprocess.run(['kubectl','apply','-f','-'],input=json.dumps(obj),text=True,check=True)
records = state.get('rbac')
if records is None:
    name='consul-endpoints-'+state['name'].rsplit('-',1)[-1]
    records=[{'kind':k,'name':name,'prior':None} for k in ('rolebinding','role')]
for record in sorted(records,key=lambda r:r['kind']!='rolebinding'):
    if record['prior'] is None:
        subprocess.run(['kubectl','-n',ns,'delete',record['kind'],record['name'],'--ignore-not-found'],check=True)
    else:
        obj=record['prior']
        for key in ['resourceVersion','uid','creationTimestamp','managedFields']:
            obj['metadata'].pop(key,None)
        subprocess.run(['kubectl','apply','--validate=false','-f','-'],input=json.dumps(obj),text=True,check=True)
PY

#!/usr/bin/env bash
set -euo pipefail
NAMESPACE="${NAMESPACE:-hotel-reservation}"
ARTIFACT_DIR="${ARTIFACT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}"
export NAMESPACE ARTIFACT_DIR

python3 - <<'PY'
import hashlib, json, os, subprocess
ns = os.environ['NAMESPACE']
directory = os.environ['ARTIFACT_DIR']
def get(resource, *args):
    return json.loads(subprocess.check_output(['kubectl','-n',ns,'get',resource,*args,'-o','json']))
service = get('service','frontend')
pods = get('pods')['items']
peers = {'frontend','search','rate','profile','geo','recommendation','reservation','user','consul'}
endpoints = []
found = set()
for pod in sorted(pods, key=lambda p:p['metadata']['name']):
    component = pod['metadata'].get('labels',{}).get('io.kompose.service')
    if component not in peers or pod['metadata'].get('deletionTimestamp'):
        continue
    if pod['status'].get('phase') != 'Running' or not any(c['type']=='Ready' and c['status']=='True' for c in pod['status'].get('conditions',[])):
        continue
    address = pod['status'].get('podIP')
    if not address:
        continue
    found.add(component)
    endpoints.append({'addresses':[address], 'conditions':{'ready':True,'serving':True,'terminating':False},
                      'nodeName':pod['spec']['nodeName'],
                      'targetRef':{'kind':'Pod','namespace':ns,'name':pod['metadata']['name'],'uid':pod['metadata']['uid']}})
if found != peers:
    raise SystemExit('Application endpoint inventory is not ready: '+', '.join(sorted(peers-found)))
uid = service['metadata']['uid']
name = 'frontend-'+hashlib.sha256((uid+'/http').encode()).hexdigest()[:5]
port = next(p for p in service['spec']['ports'] if p['port']==5000)
obj = {'apiVersion':'discovery.k8s.io/v1','kind':'EndpointSlice','metadata':{
    'name':name,'namespace':ns,
    'labels':{'io.kompose.service':'frontend','kubernetes.io/service-name':'frontend',
              'endpointslice.kubernetes.io/managed-by':'consul.hashicorp.com'},
    'ownerReferences':[{'apiVersion':'v1','kind':'Service','name':'frontend','uid':uid,'controller':True,'blockOwnerDeletion':True}]},
    'addressType':'IPv4','ports':[{'name':port['name'],'port':8500,'protocol':'TCP'}], 'endpoints':endpoints}
state_path = os.path.join(directory,'routing-state.json')
saved = None
if os.path.exists(state_path):
    with open(state_path) as f:
        saved = json.load(f)
if not saved or saved.get('service_uid') != uid or saved.get('namespace') != ns:
    prior = subprocess.run(['kubectl','-n',ns,'get','endpointslice',name,'-o','json','--ignore-not-found'],capture_output=True,text=True,check=True).stdout.strip()
    with open(state_path,'w') as f:
        json.dump({'namespace':ns,'service_uid':uid,'name':name,'prior':json.loads(prior) if prior else None},f)
with open(state_path) as f:
    saved = json.load(f)
role_name = 'consul-endpoints-'+name.rsplit('-',1)[-1]
if 'rbac' not in saved:
    saved['rbac'] = []
    for kind in ('role','rolebinding'):
        prior = subprocess.run(['kubectl','-n',ns,'get',kind,role_name,'-o','json','--ignore-not-found'],capture_output=True,text=True,check=True).stdout.strip()
        saved['rbac'].append({'kind':kind,'name':role_name,'prior':json.loads(prior) if prior else None})
    with open(state_path,'w') as f:
        json.dump(saved,f)
role_metadata = {'name':role_name,'namespace':ns,'labels':{'io.kompose.service':'consul'}}
role = {'apiVersion':'rbac.authorization.k8s.io/v1','kind':'Role','metadata':role_metadata,'rules':[
    {'apiGroups':[''],'resources':['pods'],'verbs':['get','list']},
    {'apiGroups':[''],'resources':['services'],'resourceNames':['frontend'],'verbs':['get']},
    {'apiGroups':['discovery.k8s.io'],'resources':['endpointslices'],'resourceNames':[name],'verbs':['get','update','patch']},
    {'apiGroups':['discovery.k8s.io'],'resources':['endpointslices'],'verbs':['create']}]}
binding = {'apiVersion':'rbac.authorization.k8s.io/v1','kind':'RoleBinding','metadata':role_metadata,
    'roleRef':{'apiGroup':'rbac.authorization.k8s.io','kind':'Role','name':role_name},
    'subjects':[{'kind':'ServiceAccount','name':'default','namespace':ns}]}
subprocess.run(['kubectl','apply','--validate=false','-f','-'],input=json.dumps({'apiVersion':'v1','kind':'List','items':[role,binding]}),text=True,check=True)
subprocess.run(['kubectl','apply','-f','-'],input=json.dumps(obj),text=True,check=True)
live = get('endpointslice',name)
assert live['ports'][0]['port'] == 8500
assert live['metadata']['labels']['kubernetes.io/service-name'] == 'frontend'
assert len(live['endpoints']) == len(endpoints)
with open(os.path.join(directory,'endpoint-export.py')) as f:
    source = f.read()
configuration = {'namespace':ns,'service':'frontend','name':name,'port':8500,'members':sorted(peers),'interval':120}
bootstrap = r'''
import json, os, signal, subprocess, sys
payload=json.load(sys.stdin)
directory='/tmp/consul-endpoints'
os.makedirs(directory,exist_ok=True)
script=directory+'/endpoint-export.py'
pidfile=directory+'/export.pid'
running=False
if os.path.exists(pidfile):
    pid=int(open(pidfile).read())
    try:
        os.kill(pid,0)
        running=script.encode() in open('/proc/'+str(pid)+'/cmdline','rb').read()
    except (ProcessLookupError,FileNotFoundError): pass
if not running and os.path.exists(script):
    if '/apis/discovery.k8s.io/v1/namespaces/' not in open(script).read():
        raise SystemExit('Endpoint export directory is occupied')
with open(directory+'/registration.json.tmp','w') as f: json.dump(payload['config'],f)
os.replace(directory+'/registration.json.tmp',directory+'/registration.json')
if not running:
    with open(script,'w') as f: f.write(payload['source'])
    process=subprocess.Popen([sys.executable,script,'--state-dir',directory],stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True,close_fds=True)
    with open(pidfile,'w') as f: f.write(str(process.pid))
    os.kill(process.pid,0)
print('Endpoint export is active')
'''
subprocess.run(['kubectl','-n',ns,'exec','-i','deployment/recommendation','--','python3','-c',bootstrap],
    input=json.dumps({'source':source,'config':configuration}),text=True,check=True)
PY

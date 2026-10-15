#!/usr/bin/env bash
set -euo pipefail
NAMESPACE="${NAMESPACE:-hotel-reservation}"
export NAMESPACE
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/routing-lib.sh"
stop_endpoint_exports

python3 - <<'PY'
import json, os, subprocess
ns = os.environ['NAMESPACE']
def get(resource, *args):
    return json.loads(subprocess.check_output(['kubectl','-n',ns,'get',resource,*args,'-o','json']))
service = get('service','frontend')
pods = get('pods')['items']
selector = service['spec']['selector']
matching = {p['status'].get('podIP'):p for p in pods if all(p['metadata'].get('labels',{}).get(k)==v for k,v in selector.items())}
service_ports = {(p.get('name',''),p.get('protocol','TCP')):p for p in service['spec']['ports']}
roles = get('roles')['items']
bindings = get('rolebindings')['items']
for role in roles:
    owns_frontend = any(o.get('uid')==service['metadata']['uid'] for o in role['metadata'].get('ownerReferences',[]))
    reads_frontend = any('services' in r.get('resources',[]) and 'frontend' in r.get('resourceNames',[]) for r in role.get('rules',[]))
    if not (owns_frontend or reads_frontend):
        continue
    if not any('discovery.k8s.io' in r.get('apiGroups',[]) and 'endpointslices' in r.get('resources',[]) and ('update' in r.get('verbs',[]) or 'create' in r.get('verbs',[])) for r in role.get('rules',[])):
        continue
    for binding in bindings:
        if binding['roleRef'].get('kind')=='Role' and binding['roleRef']['name']==role['metadata']['name']:
            subprocess.run(['kubectl','-n',ns,'delete','rolebinding',binding['metadata']['name'],'--ignore-not-found'],check=True)
    subprocess.run(['kubectl','-n',ns,'delete','role',role['metadata']['name'],'--ignore-not-found'],check=True)
for item in get('endpointslices','-l','kubernetes.io/service-name=frontend')['items']:
    invalid = False
    for ep in item.get('endpoints',[]):
        for address in ep['addresses']:
            pod = matching.get(address)
            if not pod:
                invalid = True
                continue
            for port in item.get('ports',[]):
                spec = service_ports.get((port.get('name',''),port.get('protocol','TCP')))
                if not spec:
                    invalid = True
                    continue
                target = spec.get('targetPort',spec['port'])
                if isinstance(target,str):
                    candidates = [p['containerPort'] for c in pod['spec']['containers'] for p in c.get('ports',[]) if p.get('name')==target and p.get('protocol','TCP')==spec.get('protocol','TCP')]
                    if port.get('port') not in candidates:
                        invalid = True
                elif port.get('port') != target:
                    invalid = True
    if invalid:
        subprocess.run(['kubectl','-n',ns,'delete','endpointslice',item['metadata']['name'],'--ignore-not-found','--wait=true'],check=True)
PY

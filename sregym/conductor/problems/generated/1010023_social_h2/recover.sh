#!/bin/bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-social-network}
ARTIFACT_DIR=${ARTIFACT_DIR:-$(cd "$(dirname "$0")" && pwd)}
cd "$ARTIFACT_DIR"
python3 - <<'PY'
import json
c=json.load(open('original-cm.json')); d=json.load(open('original-deploy.json'))
json.dump([{'op':'replace','path':'/data','value':c['data']}],open('restore-cm.json','w'))
json.dump([{'op':'replace','path':'/spec/template','value':d['spec']['template']}],open('restore-deploy.json','w'))
PY
kubectl patch cm nginx-thrift -n "$NAMESPACE" --type json --patch-file restore-cm.json
kubectl patch deploy nginx-thrift -n "$NAMESPACE" --type json --patch-file restore-deploy.json
kubectl rollout status deploy/nginx-thrift -n "$NAMESPACE" --timeout=180s
kubectl get rs -n "$NAMESPACE" -l service=nginx-thrift -o json > recovery-replicasets.json
python3 - <<'PY'
import copy,json
original=json.load(open('original-deploy.json'))
def norm(t):
 t=copy.deepcopy(t);t['metadata'].get('labels',{}).pop('pod-template-hash',None);return t
names=[]
for r in json.load(open('recovery-replicasets.json'))['items']:
 if any(o.get('uid')==original['metadata']['uid'] for o in r['metadata'].get('ownerReferences',[])) and r['spec'].get('replicas',0)==0 and norm(r['spec']['template'])!=norm(original['spec']['template']):
  names.append(r['metadata']['name'])
open('retired-replicasets.txt','w').write('\n'.join(names))
PY
while IFS= read -r name || [[ -n "$name" ]]; do
 [[ -z "$name" ]] || kubectl delete rs "$name" -n "$NAMESPACE" --ignore-not-found=true
done < retired-replicasets.txt

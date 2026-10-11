#!/bin/bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-social-network}
ARTIFACT_DIR=${ARTIFACT_DIR:-$(cd "$(dirname "$0")" && pwd)}
cd "$ARTIFACT_DIR"
python3 - <<'PY'
import json
c=json.load(open('original-cm.json')); d=json.load(open('original-deploy.json'))
json.dump({'data':c['data']},open('restore-cm.json','w'))
json.dump({'spec':{'template':d['spec']['template']}},open('restore-deploy.json','w'))
PY
kubectl patch cm nginx-thrift -n "$NAMESPACE" --type merge --patch-file restore-cm.json
kubectl patch deploy nginx-thrift -n "$NAMESPACE" --type merge --patch-file restore-deploy.json
kubectl rollout status deploy/nginx-thrift -n "$NAMESPACE" --timeout=180s
kubectl delete pod -n "$NAMESPACE" -l service=nginx-thrift --wait=true
kubectl rollout status deploy/nginx-thrift -n "$NAMESPACE" --timeout=180s
kubectl get rs -n "$NAMESPACE" -l service=nginx-thrift -o json > recovery-replicasets.json
python3 - <<'PY'
import copy,json
original=json.load(open('original-deploy.json'))
def normalized(template):
 x=copy.deepcopy(template)
 x['metadata'].get('labels',{}).pop('pod-template-hash',None)
 return x
names=[]
for rs in json.load(open('recovery-replicasets.json'))['items']:
 owned=any(o.get('uid')==original['metadata']['uid'] for o in rs['metadata'].get('ownerReferences',[]))
 if owned and rs['spec'].get('replicas',0)==0 and normalized(rs['spec']['template'])!=normalized(original['spec']['template']):
  names.append(rs['metadata']['name'])
open('retired-replicasets.txt','w').write('\n'.join(names))
PY
while IFS= read -r name || [[ -n "$name" ]]; do
  [[ -z "$name" ]] || kubectl delete rs "$name" -n "$NAMESPACE" --ignore-not-found=true
done < retired-replicasets.txt

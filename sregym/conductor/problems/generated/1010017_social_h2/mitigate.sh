#!/bin/bash
set -euo pipefail
export NAMESPACE="${NAMESPACE:-social-network}"
python3 - <<'PY'
import os,json,re,subprocess
ns=os.environ['NAMESPACE']
def k(*a): return subprocess.check_output(['kubectl','-n',ns,*a],text=True)
# Decimal128 IDs retain their exact integer values. Restore the reader's BSON contract.
print(k('exec','deployment/post-storage-mongodb','--','mongo','--quiet','--eval', '''
db=db.getSiblingDB('post');
var r=db.post.updateMany({'media.media_id':{$type:'decimal'}},[{$set:{media:{$map:{input:'$media',as:'m',in:{$mergeObjects:['$$m',{media_id:{$cond:[{$eq:[{$type:'$$m.media_id'},'decimal']},{$toLong:'$$m.media_id'},'$$m.media_id']}}]}}}}}]);
assert(r.acknowledged);
printjson(r);
'''))
c=json.loads(k('get','cm','post-storage-service','-o','json'));s=c['data']['service-config.json']
s,n=re.subn(r'("post-storage-mongodb"\s*:\s*\{[^{}]*?"connections"\s*:\s*)\d+',r'\g<1>512',s)
assert n==1
if s!=c['data']['service-config.json']:
 print(k('patch','cm','post-storage-service','--type=merge','-p',json.dumps({'data':{'service-config.json':s}})))
PY
kubectl -n "$NAMESPACE" exec deployment/nginx-thrift -c nginx-thrift -- bash -c 'exec 3<>/dev/tcp/post-storage-memcached/11211; printf "flush_all\r\nquit\r\n" >&3; cat <&3'
# Live processes retain outstanding operations and unavailable client leases.
for service in post-storage-service; do
  kubectl -n "$NAMESPACE" delete pod -l "service=$service" --wait=true
  kubectl -n "$NAMESPACE" rollout status "deployment/$service" --timeout=180s
  kubectl -n "$NAMESPACE" wait --for=condition=Ready pod -l "service=$service" --timeout=180s
done

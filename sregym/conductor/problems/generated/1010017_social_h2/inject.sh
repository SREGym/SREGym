#!/bin/bash
set -euo pipefail
export NAMESPACE="${NAMESPACE:-social-network}"
export ARTIFACT_DIR="${ARTIFACT_DIR:-$(cd "$(dirname "$0")" && pwd)}"
python3 - <<'PY'
import os,json,re,subprocess
ns=os.environ['NAMESPACE']; root=os.environ['ARTIFACT_DIR']
def k(*a): return subprocess.check_output(['kubectl','-n',ns,*a],text=True)
c=json.loads(k('get','cm','post-storage-service','-o','json'))
p=os.path.join(root,'original-config.json')
saved=json.load(open(p)) if os.path.exists(p) else {}
if saved.get('uid')!=c['metadata']['uid']:
 with open(p,'w') as f: json.dump({'uid':c['metadata']['uid'],'data':c['data']},f)
s=c['data']['service-config.json']
s,n=re.subn(r'("post-storage-mongodb"\s*:\s*\{[^{}]*?"connections"\s*:\s*)\d+',r'\g<1>16',s)
assert n==1
# Widen the existing numeric field without changing its numeric value.
print(k('exec','deployment/post-storage-mongodb','--','mongo','--quiet','--eval', '''
db=db.getSiblingDB('post');
var r=db.post.updateMany({'media.media_id':{$type:'long'}},[{$set:{media:{$map:{input:'$media',as:'m',in:{$mergeObjects:['$$m',{media_id:{$cond:[{$eq:[{$type:'$$m.media_id'},'long']},{$toDecimal:'$$m.media_id'},'$$m.media_id']}}]}}}}}]);
assert(r.acknowledged);
printjson(r);
'''))
if s!=c['data']['service-config.json']:
 print(k('patch','cm','post-storage-service','--type=merge','-p',json.dumps({'data':{'service-config.json':s}})))
 print(k('delete','pod','-l','service=post-storage-service','--wait=true'))
PY
kubectl -n "$NAMESPACE" rollout status deployment/post-storage-service --timeout=180s
kubectl -n "$NAMESPACE" wait --for=condition=Ready pod -l service=post-storage-service --timeout=180s
# Retire cached encodings so the widened storage schema is used consistently.
kubectl -n "$NAMESPACE" exec deployment/nginx-thrift -c nginx-thrift -- bash -c 'exec 3<>/dev/tcp/post-storage-memcached/11211; printf "flush_all\r\nquit\r\n" >&3; cat <&3'

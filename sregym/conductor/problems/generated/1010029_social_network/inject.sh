#!/bin/bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-social-network}
ARTIFACT_DIR=${ARTIFACT_DIR:-$(cd "$(dirname "$0")" && pwd)}
export NAMESPACE ARTIFACT_DIR
python3 - <<'PYCODE'
import os,json,subprocess
ns=os.environ['NAMESPACE']; base=os.environ['ARTIFACT_DIR']
d=json.loads(subprocess.check_output(['kubectl','-n',ns,'get','cm','nginx-thrift','-o','json']))
c=d['data']['nginx.conf']; p=os.path.join(base,'original-config.json')
saved=json.load(open(p)) if os.path.exists(p) else {}
if saved.get('uid')!=d['metadata']['uid']:
    json.dump({'nginx.conf':c,'uid':d['metadata']['uid']},open(p,'w'))
block=open(os.path.join(base,'transport.txt')).read()
if 'local batch_size = 256' not in c:
    c=c.replace('    local config = ngx.shared.config;',block+'\n    local config = ngx.shared.config;')
else:
    c=c.replace('math.ceil(#buf / batch_size)','math.floor(#buf / batch_size)')
if c!=d['data']['nginx.conf']:
    subprocess.check_call(['kubectl','-n',ns,'patch','cm','nginx-thrift','--type=merge','-p',json.dumps({'data':{'nginx.conf':c}})])
    subprocess.check_call(['kubectl','-n',ns,'delete','pod','-l','service=nginx-thrift','--wait=true'])
PYCODE
kubectl -n "$NAMESPACE" rollout status deploy/nginx-thrift --timeout=180s
kubectl -n "$NAMESPACE" wait pod -l service=nginx-thrift --for=condition=Ready --timeout=180s

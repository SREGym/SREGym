#!/bin/bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-social-network}
export NAMESPACE
python3 - <<'PYCODE'
import os,json,subprocess
ns=os.environ['NAMESPACE']
d=json.loads(subprocess.check_output(['kubectl','-n',ns,'get','cm','nginx-thrift','-o','json']))
c=d['data']['nginx.conf']; fixed=c.replace('math.floor(#buf / batch_size)','math.ceil(#buf / batch_size)')
if fixed!=c:
    subprocess.check_call(['kubectl','-n',ns,'patch','cm','nginx-thrift','--type=merge','-p',json.dumps({'data':{'nginx.conf':fixed}})])
    subprocess.check_call(['kubectl','-n',ns,'delete','pod','-l','service=nginx-thrift','--wait=true'])
PYCODE
kubectl -n "$NAMESPACE" rollout status deploy/nginx-thrift --timeout=180s
kubectl -n "$NAMESPACE" wait pod -l service=nginx-thrift --for=condition=Ready --timeout=180s

#!/bin/bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-social-network}
ARTIFACT_DIR=${ARTIFACT_DIR:-$(cd "$(dirname "$0")" && pwd)}
export NAMESPACE ARTIFACT_DIR
python3 - <<'PYCODE'
import os,json,subprocess
ns=os.environ['NAMESPACE']; base=os.environ['ARTIFACT_DIR']; p=os.path.join(base,'original-config.json')
if os.path.exists(p):
    saved=json.load(open(p)); data={'nginx.conf':saved['nginx.conf']}
    d=json.loads(subprocess.check_output(['kubectl','-n',ns,'get','cm','nginx-thrift','-o','json']))
    if d['data']['nginx.conf']!=data['nginx.conf']:
        subprocess.check_call(['kubectl','-n',ns,'patch','cm','nginx-thrift','--type=merge','-p',json.dumps({'data':data})])
        subprocess.check_call(['kubectl','-n',ns,'delete','pod','-l','service=nginx-thrift','--wait=true'])
PYCODE
kubectl -n "$NAMESPACE" rollout status deploy/nginx-thrift --timeout=180s
kubectl -n "$NAMESPACE" wait pod -l service=nginx-thrift --for=condition=Ready --timeout=180s

#!/bin/bash
set -euo pipefail
export NAMESPACE="${NAMESPACE:-social-network}"
bash "$(dirname "$0")/mitigate.sh"
python3 - <<'PY'
import json, os, subprocess
ns=os.environ['NAMESPACE']
cmd=['kubectl','-n',ns]
d=json.loads(subprocess.check_output(cmd+['get','deploy','nginx-thrift','-o','json'],text=True))
a=d['spec']['template']['metadata'].get('annotations',{})
if 'kubectl.kubernetes.io/restartedAt' in a:
 path='/spec/template/metadata/annotations/kubectl.kubernetes.io~1restartedAt'
 if len(a)==1: path='/spec/template/metadata/annotations'
 subprocess.check_call(cmd+['patch','deploy','nginx-thrift','--type=json','-p',json.dumps([{'op':'remove','path':path}])])

PY
kubectl -n "$NAMESPACE" rollout status deployment/nginx-thrift --timeout=180s

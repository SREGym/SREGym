#!/bin/bash
set -euo pipefail
export NAMESPACE="${NAMESPACE:-social-network}"
export ARTIFACT_DIR="${ARTIFACT_DIR:-$(cd "$(dirname "$0")" && pwd)}"
bash "$(dirname "$0")/mitigate.sh"
python3 - <<'PY'
import os,json,subprocess
p=os.path.join(os.environ['ARTIFACT_DIR'],'original-config.json')
if os.path.exists(p):
 saved=json.load(open(p));data=saved['data'];ns=os.environ['NAMESPACE']
 c=json.loads(subprocess.check_output(['kubectl','-n',ns,'get','cm','post-storage-service','-o','json']))
 if saved['uid']!=c['metadata']['uid']:
  raise RuntimeError('Configuration snapshot belongs to another deployment')
 if c['data']!=data:
  subprocess.check_call(['kubectl','-n',ns,'patch','cm','post-storage-service','--type=merge','-p',json.dumps({'data':data})])
  subprocess.check_call(['kubectl','-n',ns,'delete','pod','-l','service=post-storage-service','--wait=true'])
  subprocess.check_call(['kubectl','-n',ns,'rollout','status','deployment/post-storage-service','--timeout=180s'])
PY

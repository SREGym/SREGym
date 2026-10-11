#!/bin/bash
set -euo pipefail
export NAMESPACE="${NAMESPACE:-social-network}"
python3 - <<'PY'
import json, os, subprocess
ns=os.environ['NAMESPACE']
def k(*args): return subprocess.check_output(['kubectl','-n',ns,*args],text=True)
import re
cm=json.loads(k('get','cm','nginx-thrift','-o','json'))
conf=cm['data']['nginx.conf']
start='    -- RPC admission control\n'
end='    -- End RPC admission control\n'
clean=re.sub(re.escape(start)+r'.*?'+re.escape(end), '', conf, flags=re.S)
if clean != conf:
 k('patch','cm','nginx-thrift','--type=merge','-p',json.dumps({'data':{'nginx.conf':clean}}))
 k('rollout','restart','deploy/nginx-thrift')

PY
kubectl -n "$NAMESPACE" rollout status deployment/nginx-thrift --timeout=180s
# Retire the obsolete admission ledger after all callers use the image's pool.
pod=$(kubectl -n "$NAMESPACE" get pods -l service=social-graph-redis -o jsonpath='{.items[0].metadata.name}')
kubectl -n "$NAMESPACE" exec "$pod" -- redis-cli EVAL "local keys=redis.call('KEYS','rpc:admission:*'); for _,key in ipairs(keys) do redis.call('DEL',key) end; return #keys" 0

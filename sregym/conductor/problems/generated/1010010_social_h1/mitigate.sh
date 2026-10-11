#!/bin/bash
set -euo pipefail
export NAMESPACE="${NAMESPACE:-social-network}"
python3 - <<'PY'
import os,json,subprocess,re
ns=os.environ['NAMESPACE']
def k(*a): return subprocess.check_output(['kubectl','-n',ns,*a],text=True)
cm=json.loads(k('get','cm','nginx-thrift','-o','json'))
conf=cm['data']['nginx.conf']
conf=re.sub(r'    -- RPC completion adapter\n.*?    -- End RPC completion adapter\n','',conf,flags=re.S)
conf=re.sub(r'    (?:--|#) Transport session initialization\n.*?    (?:--|#) End Transport session initialization\n\n','',conf,flags=re.S)
if conf != cm['data']['nginx.conf']:
 k('patch','cm','nginx-thrift','--type=merge','-p',json.dumps({'data':{'nginx.conf':conf}}))
 for p in json.loads(k('get','pods','-l','service=nginx-thrift','-o','json'))['items']:
  k('delete','pod',p['metadata']['name'],'--wait=true')
PY
kubectl -n "$NAMESPACE" rollout status deployment/nginx-thrift --timeout=180s
kubectl -n "$NAMESPACE" wait --for=condition=Ready pod -l service=nginx-thrift --timeout=180s
kubectl -n "$NAMESPACE" exec deployment/social-graph-redis -- redis-cli EVAL "local count=0; for _,pattern in ipairs({'rpc:sessions:*','rpc:session-receipts:*'}) do local cursor='0'; repeat local result=redis.call('SCAN',cursor,'MATCH',pattern,'COUNT',100); cursor=result[1]; for _,key in ipairs(result[2]) do count=count+redis.call('DEL',key) end until cursor=='0'; end; return count" 0

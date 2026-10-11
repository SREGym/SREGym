#!/usr/bin/env bash
set -euo pipefail
export NAMESPACE="${NAMESPACE:-social-network}"
export ARTIFACT_DIR="${ARTIFACT_DIR:-$(pwd)}"
python3 - <<'PY'
import json, os, subprocess
ns=os.environ['NAMESPACE']; directory=os.environ['ARTIFACT_DIR']
def mongo(js):
    return subprocess.check_output(['kubectl','exec','-n',ns,'deploy/post-storage-mongodb','--','mongo','--quiet','--eval',js],text=True)
state=os.path.join(directory,'original-policy.json')
namespace_uid=json.loads(subprocess.check_output(['kubectl','get','namespace',ns,'-o','json'],text=True))['metadata']['uid']
previous=json.load(open(state)) if os.path.exists(state) else {}
if previous.get('namespace_uid') != namespace_uid:
    raw=mongo('var d=db.getSiblingDB("post"); var c=d.getCollectionInfos({name:"post"})[0]; if(!c) throw new Error("post collection unavailable"); print(JSON.stringify({options:c.options,indexes:d.post.getIndexes()}));')
    saved=json.loads(raw.strip())
    saved['namespace_uid']=namespace_uid
    with open(state+'.tmp','w') as f: json.dump(saved,f)
    os.replace(state+'.tmp',state)
original=json.load(open(state))['options'].get('validator',{})
n={'$strLenCP':'$text'}
span={'$substrCP':['$text','$$start',{'$subtract':['$$end','$$start']}]}
budgets={'$and':[{'$lte':[{'$strLenBytes':span},4096]}, {'$lte':[{'$strLenCP':span},2048]}, {'$lte':[{'$size':{'$split':[span,' ']}},256]}]}
ends={'$allElementsTrue':{'$map':{'input':{'$range':[{'$add':['$$start',1]},{'$add':[n,1]},1]},'as':'end','in':budgets}}}
policy={'$expr':{'$allElementsTrue':{'$map':{'input':{'$range':[0,n,1]},'as':'start','in':ends}}}}
if original: policy={'$and':[original,policy]}
command={'collMod':'post','validator':policy,'validationLevel':'strict','validationAction':'error'}
print(mongo('var r=db.getSiblingDB("post").runCommand('+json.dumps(command)+'); if(!r.ok) throw new Error(JSON.stringify(r)); print(JSON.stringify(r));').strip())
PY

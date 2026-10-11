#!/usr/bin/env bash
set -euo pipefail
export NAMESPACE="${NAMESPACE:-social-network}"
python3 - <<'PY'
import json,os,subprocess
ns=os.environ['NAMESPACE']
def mongo(js):
    return subprocess.check_output(['kubectl','exec','-n',ns,'deploy/post-storage-mongodb','--','mongo','--quiet','--eval',js],text=True)
options=json.loads(mongo('print(JSON.stringify(db.getSiblingDB("post").getCollectionInfos({name:"post"})[0].options));').strip())
validator=options.get('validator',{})
# Preserve any independent collection policy; replace the scalar text budget only.
clauses=validator.get('$and',[validator])
replacement=[]
for clause in clauses:
    expr=clause.get('$expr',{})
    if not isinstance(expr,dict) or '$allElementsTrue' not in expr:
        replacement.append(clause); continue
    checks=[]
    def visit(node):
        if isinstance(node,dict):
            pair=node.get('$lte')
            if isinstance(pair,list) and len(pair)==2 and isinstance(pair[1],(int,float)) and isinstance(pair[0],dict):
                lhs,bound=pair
                if '$strLenBytes' in lhs: checks.append({'$lte':[{'$strLenBytes':'$text'},bound]})
                elif '$strLenCP' in lhs: checks.append({'$lte':[{'$strLenCP':'$text'},bound]})
                elif '$size' in lhs and isinstance(lhs['$size'],dict) and '$split' in lhs['$size']:
                    delimiter=lhs['$size']['$split'][1]
                    checks.append({'$lte':[{'$size':{'$split':['$text',delimiter]}},bound]})
            for value in node.values(): visit(value)
        elif isinstance(node,list):
            for value in node: visit(value)
    visit(expr)
    if not checks: raise RuntimeError('Unable to identify scalar text budgets')
    replacement.append({'$expr':{'$and':checks}})
policy=replacement[0] if len(replacement)==1 else {'$and':replacement}
r={'collMod':'post','validator':policy,'validationLevel':options.get('validationLevel','strict'),'validationAction':options.get('validationAction','error')}
print(mongo('var r=db.getSiblingDB("post").runCommand('+json.dumps(r)+'); if(!r.ok) throw new Error(JSON.stringify(r)); print(JSON.stringify(r));').strip())
PY

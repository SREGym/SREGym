#!/usr/bin/env bash
set -euo pipefail
export NAMESPACE="${NAMESPACE:-social-network}"
export ARTIFACT_DIR="${ARTIFACT_DIR:-$(pwd)}"
python3 - <<'PY'
import json,os,subprocess
ns=os.environ['NAMESPACE']; saved=json.load(open(os.path.join(os.environ['ARTIFACT_DIR'],'original-policy.json')))
def kube(*args): return subprocess.check_output(['kubectl','-n',ns,*args],text=True)
def mongo(js): return kube('exec','deploy/post-storage-mongodb','--','mongo','--quiet','--eval',js)
options=saved['options']
if all(k in options for k in ['validator','validationLevel','validationAction']):
    cmd=dict(collMod='post',validator=options['validator'],validationLevel=options['validationLevel'],validationAction=options['validationAction'])
    print(mongo('var r=db.getSiblingDB("post").runCommand('+json.dumps(cmd)+');if(!r.ok)throw new Error(JSON.stringify(r));print(JSON.stringify(r));').strip())
else:
    # MongoDB cannot unset explicit validation metadata with collMod. A lossless
    # collection rebuild restores the original options, documents and indexes.
    replicas={name:json.loads(kube('get','deploy',name,'-o','json'))['spec']['replicas'] for name in ['nginx-thrift','post-storage-service']}
    try:
        for name in replicas:
            kube('scale','deploy/'+name,'--replicas=0')
            kube('wait','--for=delete','pod','-l','service='+name,'--timeout=180s')
        js='''var d=db.getSiblingDB("post");
var original=OPTIONS;
var indexes=INDEXES;
var tmp="post_rebuild";
if(d.getCollectionInfos({name:tmp}).length) d.getCollection(tmp).drop();
var r=d.createCollection(tmp,original); if(!r.ok) throw new Error(JSON.stringify(r));
var batch=[]; var count=0;
d.post.find().forEach(function(doc){batch.push(doc);count++;if(batch.length>=500){d.getCollection(tmp).insertMany(batch);batch=[];}});
if(batch.length) d.getCollection(tmp).insertMany(batch);
indexes.forEach(function(index){if(index.name==="_id_")return;var key=index.key;delete index.key;delete index.v;delete index.ns;d.getCollection(tmp).createIndex(key,index);});
if(d.getCollection(tmp).countDocuments({})!==count) throw new Error("Collection copy count mismatch");
r=d.getCollection(tmp).renameCollection("post",true); if(!r.ok) throw new Error(JSON.stringify(r));
print(JSON.stringify({ok:1,documents:count}));'''.replace('OPTIONS',json.dumps(options)).replace('INDEXES',json.dumps(saved['indexes']))
        print(mongo(js).strip())
    finally:
        for name,number in reversed(list(replicas.items())):
            kube('scale','deploy/'+name,'--replicas='+str(number))
            if number: kube('rollout','status','deploy/'+name,'--timeout=180s')
PY

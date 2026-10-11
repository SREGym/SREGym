import json,os,subprocess,sys,pathlib
ns=os.environ.get('NAMESPACE','hotel-reservation')
base=pathlib.Path(__file__).resolve().parent
mode=sys.argv[1]
def run(*args,input=None):
 return subprocess.check_output(['kubectl','-n',ns,*args],input=input)
def get(kind,name): return json.loads(run('get',kind,name,'-o','json'))
def apply(obj): run('apply','-f','-',input=json.dumps(obj).encode())
def patch(kind,name,payload): run('patch',kind,name,'--type=merge','-p',json.dumps(payload))
def api(path,method='GET',data=None):
 if method=='GET': return json.loads(run('exec','deployment/consul','-c','consul','--','wget','-qO-','http://127.0.0.1:8500'+path))
 pods=json.loads(run('get','pods','-l','io.kompose.service=consul','-o','json'))['items']
 pod=next(p for p in pods if not p['metadata'].get('deletionTimestamp') and p['status'].get('phase')=='Running')
 ip=pod['status']['podIP']
 code='import urllib.request,sys; r=urllib.request.Request("http://"+sys.argv[1]+":8500"+sys.argv[2],data=sys.stdin.buffer.read(),method=sys.argv[3],headers={"Content-Type":"application/json"}); print(urllib.request.urlopen(r,timeout=30).read().decode())'
 return run('exec','-i','deployment/frontend','--','python3','-c',code,ip,path,method,input=json.dumps(data).encode())
def prune_search():
 pods=json.loads(run('get','pods','-l','io.kompose.service=search','-o','json'))['items']
 allowed={p['status'].get('podIP') for p in pods if not p['metadata'].get('deletionTimestamp')}
 for record in registrations().values():
  if record['Service']=='srv-search' and record['Address'] not in allowed:
   api('/v1/agent/service/deregister/'+record['ID'],'PUT',{})

def refresh_frontend():
 run('delete','pods','-l','io.kompose.service=frontend','--wait=true','--timeout=120s')
 run('rollout','status','deployment/frontend','--timeout=180s')

def registrations(): return api('/v1/agent/services')
def restore(regs):
 remote='''import json,sys,urllib.request,time
records=json.load(sys.stdin)
for n in range(120):
 try:
  urllib.request.urlopen('http://127.0.0.1:8500/v1/status/leader',timeout=2); break
 except Exception:
  if n==119: raise
  time.sleep(1)
for s in records.values():
 payload={k:s[k] for k in ('ID','Address','Port','Tags','Meta','Weights','EnableTagOverride') if k in s}
 payload['Name']=s['Service']
 req=urllib.request.Request('http://127.0.0.1:8500/v1/agent/service/register',data=json.dumps(payload).encode(),method='PUT',headers={'Content-Type':'application/json'})
 urllib.request.urlopen(req,timeout=20).read()
'''
 # frontend Python accesses the new native pod IP rather than the service cache route.
 pods=json.loads(run('get','pods','-l','io.kompose.service=consul','-o','json'))['items']
 pod=next(p for p in pods if not p['metadata'].get('deletionTimestamp') and p['status'].get('phase')=='Running')
 remote=remote.replace('127.0.0.1',pod['status']['podIP'])
 run('exec','-i','deployment/frontend','--','python3','-c',remote,input=json.dumps(regs).encode())
if mode=='inject':
 dep=get('deployment','consul')
 svc=get('service','consul')
 state=base/('.original-'+dep['metadata']['uid']+'.json')
 if not state.exists(): state.write_text(json.dumps({'template':dep['spec']['template'],'ports':svc['spec']['ports'],'search_template':get('deployment','search')['spec']['template']}))
 regs=registrations()
 cm={'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'consul-query-cache'},'data':{'server.py':(base/'query_cache.py').read_text(),'key.py':'def cache_key(path, query):\n return (path.rsplit("/", 1)[0], query.get("dc", ["dc1"])[0], "passing" in query)\n'}}
 apply(cm)
 template=dep['spec']['template']
 ps=template['spec']
 ps['containers']=[c for c in ps['containers'] if c['name']!='query-cache']
 ps['containers'].append({'name':'query-cache','image':'python:3.11-alpine','command':['python3','/etc/discovery/server.py'],'ports':[{'containerPort':8501,'name':'query-http'}],'volumeMounts':[{'name':'query-cache','mountPath':'/etc/discovery','readOnly':True}],'resources':{'requests':{'cpu':'50m','memory':'32Mi'},'limits':{'cpu':'500m','memory':'128Mi'}},'readinessProbe':{'tcpSocket':{'port':8501},'periodSeconds':2}})
 ps['volumes']=[v for v in ps.get('volumes',[]) if v['name']!='query-cache']+[{'name':'query-cache','configMap':{'name':'consul-query-cache'}}]
 patch('deployment','consul',{'spec':{'template':template}})
 run('rollout','status','deployment/consul','--timeout=240s')
 restore(regs)
 ports=svc['spec']['ports']
 for p in ports:
  if p['port']==8500: p['targetPort']='query-http'
 patch('service','consul',{'spec':{'ports':ports}})
 # Release existing native blocking queries, then start all resolver watches through the cache.
 run('rollout','restart','deployment/search')
 run('rollout','status','deployment/search','--timeout=180s')
 prune_search()
 refresh_frontend()
elif mode=='mitigate':
 # The source of truth is intact. Bypass only the caching facade; no record deletion is needed.
 svc=get('service','consul')
 ports=svc['spec']['ports']
 for p in ports:
  if p['port']==8500: p['targetPort']=8500
 patch('service','consul',{'spec':{'ports':ports}})
 # Existing blocking HTTP connections retain the old facade until its response cycle ends.
 run('rollout','restart','deployment/search')
 run('rollout','status','deployment/search','--timeout=180s')
 prune_search()
elif mode=='recover':
 regs=registrations()
 original=json.loads((base/('.original-'+get('deployment','consul')['metadata']['uid']+'.json')).read_text())
 patch('service','consul',{'spec':{'ports':original['ports']}})
 # JSON replacement removes the added container and volume as well as their defaulted fields.
 run('patch','deployment','consul','--type=json','-p',json.dumps([{'op':'replace','path':'/spec/template','value':original['template']}]))
 run('rollout','status','deployment/consul','--timeout=240s')
 restore(regs)
 run('delete','configmap','consul-query-cache','--ignore-not-found=true')
 run('patch','deployment','search','--type=json','-p',json.dumps([{'op':'replace','path':'/spec/template','value':original['search_template']}]))
 run('rollout','status','deployment/search','--timeout=180s')
 prune_search()
 refresh_frontend()
else: raise ValueError(mode)

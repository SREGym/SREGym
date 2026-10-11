import os,sys,json,subprocess,pathlib,hashlib
ns=os.environ.get('NAMESPACE','hotel-reservation')
root=pathlib.Path(os.environ.get('ARTIFACT_DIR', pathlib.Path(__file__).resolve().parent))
key='service-discovery/journal/srv-rate'; cache='service-discovery/materialized/srv-rate'
def k(*args, input=None):
 p=subprocess.run(['kubectl','-n',ns,*args],input=input,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
 if p.returncode: raise RuntimeError(p.stderr+p.stdout)
 return p.stdout
def get(kind,name): return json.loads(k('get',kind,name,'-o','json'))
def patch(kind,name,body): return k('patch',kind,name,'--type=merge','-p',json.dumps(body))
def apply(body): return k('apply','-f','-',input=json.dumps(body))
def consul(*args): return k('exec','deploy/consul','-c','consul','--','consul',*args)
def ready(name): k('rollout','status','deploy/'+name,'--timeout=300s')
def services(): return json.loads(k('exec','deploy/consul','-c','consul','--','wget','-qO-','http://127.0.0.1:8500/v1/agent/services'))
def restore_services(saved):
 for sid,s in saved.items():
  if s['Service']!='consul': consul('services','register','-id='+sid,'-name='+s['Service'],'-address='+s['Address'],'-port='+str(s['Port']))
def refresh():
 pods=json.loads(k('get','pods','-l','io.kompose.service=search','-o','json'))
 ips={p['status'].get('podIP') for p in pods['items'] if not p['metadata'].get('deletionTimestamp')}
 for sid,s in services().items():
  if s['Service']=='srv-search' and s['Address'] not in ips: consul('services','deregister','-id='+sid)
def config(source):
 return {'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'discovery-client','namespace':ns},'data':{'consul.go':source,'gateway.py':(root/'gateway.py').read_text()}}
def kv_read(path):
 p=subprocess.run(['kubectl','-n',ns,'exec','deploy/consul','-c','consul','--','consul','kv','get',path],text=True,capture_output=True)
 return p.stdout.rstrip('\n') if p.returncode==0 else None
def kv_restore(path,value):
 if value is None: consul('kv','delete',path)
 else: consul('kv','put',path,value)
def replace_template(name,t):
 k('patch','deploy',name,'--type=json','-p',json.dumps([{'op':'replace','path':'/spec/template','value':t}]))
action=sys.argv[1]; d=get('deploy','search'); state=root/'original.json'
if action=='inject':
 if not state.exists() or json.loads(state.read_text())['uid']!=d['metadata']['uid']:
  state.write_text(json.dumps({'uid':d['metadata']['uid'],'template':d['spec']['template'],'consul':get('deploy','consul')['spec']['template'],'ports':get('svc','consul')['spec']['ports'],'frontend':get('deploy','frontend')['spec']['template'],'journal':kv_read(key),'snapshot':kv_read(cache)}))
 saved=json.loads(state.read_text())
 # Support baseline captured by an earlier local iteration.
 if 'consul' not in saved:
  saved.update({'consul':get('deploy','consul')['spec']['template'],'ports':get('svc','consul')['spec']['ports'],'frontend':get('deploy','frontend')['spec']['template'],'snapshot':kv_read(cache)})
  state.write_text(json.dumps(saved))
 if 'consul_strategy' not in saved:
  saved['consul_strategy']=get('deploy','consul')['spec']['strategy']
  existing=json.loads(k('get','pvc','-o','json'))['items']
  saved['created_volume']=not any(p['metadata']['name']=='consul-state' for p in existing)
  state.write_text(json.dumps(saved))
 apply({'apiVersion':'v1','kind':'PersistentVolumeClaim','metadata':{'name':'consul-state','namespace':ns},'spec':{'accessModes':['ReadWriteOnce'],'resources':{'requests':{'storage':'1Gi'}}}})
 source=(root/'resolver.go').read_text(); apply(config(source))
 registry=services(); ct=json.loads(json.dumps(saved['consul'])); cs=ct['spec']
 cs.setdefault('volumes',[]).extend([{'name':'discovery-client','configMap':{'name':'discovery-client'}},{'name':'consul-state','persistentVolumeClaim':{'claimName':'consul-state'}}])
 cs['containers'][0]['args']=['agent','-server','-bootstrap-expect=1','-node=registry-primary','-client=0.0.0.0','-data-dir=/consul/state']
 cs['containers'][0].setdefault('volumeMounts',[]).append({'name':'consul-state','mountPath':'/consul/state'})
 cs['containers'].append({'name':'discovery-api','image':'python:3.11-alpine','command':['python3','/opt/discovery/gateway.py'],'ports':[{'name':'discovery-http','containerPort':18500}],'volumeMounts':[{'name':'discovery-client','mountPath':'/opt/discovery','readOnly':True}],'readinessProbe':{'exec':{'command':['python3','-c',"import urllib.request,json; assert json.load(urllib.request.urlopen('http://127.0.0.1:8500/v1/status/leader'))"]},'periodSeconds':2},'resources':{'requests':{'cpu':'50m'},'limits':{'cpu':'1'}}})
 ct.setdefault('metadata',{}).setdefault('annotations',{})['discovery.build']=hashlib.sha256((root/'gateway.py').read_bytes()).hexdigest()
 changed=get('deploy','consul')['spec']['template']!=ct
 patch('deploy','consul',{'spec':{'strategy':{'type':'Recreate','rollingUpdate':None},'template':ct}}); ready('consul')
 if changed: restore_services(registry)
 endpoint=get('svc','geo')['spec']['clusterIP']+':8083'; current=get('svc','rate')['spec']['clusterIP']+':8084'
 journal=[{'revision':'9','operation':'upsert','endpoint':endpoint},{'revision':'10','operation':'retire','endpoint':endpoint},{'revision':'11','operation':'upsert','endpoint':current}]
 consul('kv','put',key,json.dumps(journal))
 # Materialization uses the same textual revision ordering as the client migration.
 active={current}
 for entry in sorted(journal,key=lambda e:e['revision']):
  if entry['operation']=='upsert': active.add(entry['endpoint'])
  else: active.discard(entry['endpoint'])
 consul('kv','put',cache,json.dumps({'endpoints':sorted(active),'revision':max(int(e['revision']) for e in journal)}))
 ports=json.loads(json.dumps(saved['ports']))
 for port in ports:
  if port['port']==8500: port['targetPort']='discovery-http'
 patch('svc','consul',{'spec':{'ports':ports}})
 t=json.loads(json.dumps(saved['template'])); spec=t['spec']; image=spec['containers'][0]['image']
 base='/go/src/github.com/harlow/go-micro-services/vendor/github.com/olivere/grpc/lb/consul/consul.go'
 mounts=[{'name':'discovery-client','mountPath':base,'subPath':'consul.go','readOnly':True},{'name':'runtime-bin','mountPath':'/opt/runtime'}]
 spec.setdefault('volumes',[]).extend([{'name':'discovery-client','configMap':{'name':'discovery-client'}},{'name':'runtime-bin','emptyDir':{}}])
 spec.setdefault('initContainers',[]).append({'name':'compile-runtime','image':image,'command':['sh','-ec','GOFLAGS=-mod=vendor go build -o /opt/runtime/search ./cmd/search'],'volumeMounts':mounts,'resources':{'requests':{'cpu':'100m'},'limits':{'cpu':'2'}}})
 spec['containers'][0]['command']=['/opt/runtime/search']; spec['containers'][0].setdefault('volumeMounts',[]).extend(mounts)
 t.setdefault('metadata',{}).setdefault('annotations',{})['discovery.build']=hashlib.sha256(source.encode()).hexdigest()
 patch('deploy','search',{'spec':{'template':t}}); ready('search'); refresh()
 k('rollout','restart','deploy/frontend'); ready('frontend')
elif action=='mitigate':
 cm=get('cm','discovery-client'); src=cm['data']['consul.go']
 src=src.replace('return entries[i].Revision < entries[j].Revision','left, _ := strconv.ParseUint(entries[i].Revision, 10, 64); right, _ := strconv.ParseUint(entries[j].Revision, 10, 64); return left < right')
 # Repair the deployed source using cluster state, then rebuild the durable view.
 cm['data']['consul.go']=src
 patch('cm','discovery-client',{'data':cm['data']})
 journal=json.loads(kv_read(key)); active=set()
 for entry in sorted(journal,key=lambda e:int(e['revision'])):
  if entry['operation']=='upsert': active.add(entry['endpoint'])
  else: active.discard(entry['endpoint'])
 consul('kv','put',cache,json.dumps({'endpoints':sorted(active),'revision':max(int(e['revision']) for e in journal)}))
 t=get('deploy','search')['spec']['template']; t.setdefault('metadata',{}).setdefault('annotations',{})['discovery.build']=hashlib.sha256(src.encode()).hexdigest()
 patch('deploy','search',{'spec':{'template':t}}); ready('search'); refresh()
 k('rollout','restart','deploy/frontend'); ready('frontend')
elif action=='recover':
 if state.exists():
  saved=json.loads(state.read_text()); registry=services()
  patch('svc','consul',{'spec':{'ports':saved['ports']}})
  changed=get('deploy','consul')['spec']['template']!=saved['consul']
  replace_template('consul',saved['consul']); patch('deploy','consul',{'spec':{'strategy':saved.get('consul_strategy',{'type':'RollingUpdate','rollingUpdate':{'maxSurge':'25%','maxUnavailable':'25%'}})}}); ready('consul')
  # Native Consul starts without a readiness probe; wait on its API using exec's bounded retry.
  k('exec','deploy/consul','-c','consul','--','sh','-ec','for n in $(seq 1 120); do consul info >/dev/null 2>&1 && exit 0; done; exit 1')
  if changed: restore_services(registry)
  kv_restore(key,saved['journal']); kv_restore(cache,saved['snapshot'])
  replace_template('search',saved['template']); ready('search'); refresh()
  replace_template('frontend',saved['frontend']); ready('frontend')
 k('delete','cm','discovery-client','--ignore-not-found=true')
 if state.exists() and json.loads(state.read_text()).get('created_volume'): k('delete','pvc','consul-state','--ignore-not-found=true')
else: raise RuntimeError(action)

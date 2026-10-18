#!/usr/bin/env bash

stop_endpoint_exports() {
  export NAMESPACE
  python3 - <<'PY'
import json, os, subprocess
ns = os.environ['NAMESPACE']
def get(resource,*args):
    return json.loads(subprocess.check_output(['kubectl','-n',ns,'get',resource,*args,'-o','json']))
image = get('deployment','frontend')['spec']['template']['spec']['containers'][0]['image']
scan = r'''
import json, os, signal, time
for entry in os.listdir('/proc'):
    if not entry.isdigit() or int(entry)==os.getpid(): continue
    try:
        cmd=open('/proc/'+entry+'/cmdline','rb').read().decode().split(chr(0))
        scripts=[s for s in cmd if s.endswith('.py') and os.path.isfile(s)]
        for script in scripts:
            code=open(script).read()
            if '/apis/discovery.k8s.io/v1/namespaces/' not in code or 'kubernetes.io/service-name' not in code: continue
            if '--state-dir' not in cmd: continue
            directory=cmd[cmd.index('--state-dir')+1]
            config=json.load(open(os.path.join(directory,'registration.json')))
            if config.get('service')!='frontend' or config.get('namespace')!=os.environ.get('POD_NAMESPACE',config['namespace']): continue
            os.kill(int(entry),signal.SIGTERM)
            deadline=time.monotonic()+30
            while os.path.exists('/proc/'+entry+'/stat'):
                status=open('/proc/'+entry+'/stat').read().split(') ',1)[1].split()[0]
                if status=='Z': break
                if time.monotonic()>deadline:
                    os.kill(int(entry),signal.SIGKILL)
                    break
                time.sleep(0.1)
            for filename in ('registration.json','export.pid'):
                path=os.path.join(directory,filename)
                if os.path.exists(path): os.unlink(path)
            if os.path.exists(script): os.unlink(script)
            try: os.rmdir(directory)
            except OSError: pass
            print('Stopped endpoint export in '+os.uname().nodename)
    except (FileNotFoundError,ProcessLookupError,PermissionError):
        pass
'''
for pod in get('pods')['items']:
    if pod['metadata'].get('deletionTimestamp') or pod['status'].get('phase')!='Running': continue
    for container in pod['spec']['containers']:
        if container['image']==image:
            subprocess.run(['kubectl','-n',ns,'exec',pod['metadata']['name'],'-c',container['name'],'--','python3','-c',scan],check=True)
PY
}

#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-hotel-reservation}
export NAMESPACE
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
bash ./mitigate.sh
state=$(mktemp)
patch=$(mktemp)
trap 'rm -f "$state" "$patch"' EXIT
for component in memcached-rate search; do
  old_component_pods=$(kubectl get pods -n "$NAMESPACE" -l "io.kompose.service=$component" -o custom-columns=:metadata.name --no-headers)
  kubectl get -n "$NAMESPACE" "deploy/$component" -o json > "$state"
  python3 - "$state" "$component" > "$patch" <<'PYRESTORE'
import json,sys
j=json.load(open(sys.argv[1])); original=json.loads(j['metadata']['annotations']['kubectl.kubernetes.io/last-applied-configuration'])
c=original['spec']['template']['spec']['containers'][0]; change={'name':c['name']}
if sys.argv[2]=='memcached-rate':
    change['args']=c.get('args')
else:
    change['env']=[{'$patch':'replace'}]+c.get('env',[])
print(json.dumps({'spec':{'template':{'spec':{'containers':[change]}}}}))
PYRESTORE
  patch_result=$(kubectl patch -n "$NAMESPACE" "deploy/$component" --type=strategic --patch-file "$patch")
  printf '%s\n' "$patch_result"
  kubectl rollout status -n "$NAMESPACE" "deploy/$component" --timeout=180s
  if [[ "$patch_result" != *"(no change)"* ]]; then
    for component_pod in $old_component_pods; do
      kubectl wait -n "$NAMESPACE" --for=delete "pod/$component_pod" --timeout=180s
    done
  fi
done
kubectl get -n "$NAMESPACE" deploy/rate -o json > "$state"
python3 - "$state" > "$patch" <<'PY'
import json,sys
current=json.load(open(sys.argv[1]))
original=json.loads(current['metadata']['annotations']['kubectl.kubernetes.io/last-applied-configuration'])
c=next(x for x in original['spec']['template']['spec']['containers'] if x['name']=='hotel-reserv-rate')
env=[next((x for x in c.get('env',[]) if x['name']==name),{'name':name,'$patch':'delete'}) for name in ['MEMC_TIMEOUT','GOMAXPROCS']]
container={'name':c['name'],'env':env, 'resources':{'limits':{'cpu':c.get('resources',{}).get('limits',{}).get('cpu')}}}
annotation=original['spec']['template'].get('metadata',{}).get('annotations',{}).get('kubectl.kubernetes.io/restartedAt')
print(json.dumps({'spec':{'template':{'metadata':{'annotations':{'kubectl.kubernetes.io/restartedAt':annotation}},'spec':{'containers':[container]}}}}))
PY
kubectl patch -n "$NAMESPACE" deploy/rate --type=strategic --patch-file "$patch"
kubectl rollout status -n "$NAMESPACE" deploy/rate --timeout=180s

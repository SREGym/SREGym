#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-hotel-reservation}
export NAMESPACE
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
bash ./mitigate.sh
kubectl exec -n "$NAMESPACE" deploy/mongodb-rate -c hotel-reserv-rate-mongo -- mongo --quiet -u admin -p admin --authenticationDatabase admin rate-db --eval 'db.catalog_export.drop();db.inventory_history.drop();db.inventory_current.drop()'
kubectl delete configmap catalog-refresh -n "$NAMESPACE" --ignore-not-found
state=$(mktemp)
patch=$(mktemp)
trap 'rm -f "$state" "$patch"' EXIT
for component in rate search memcached-rate; do
 kubectl get -n "$NAMESPACE" "deploy/$component" -o json > "$state"
 python3 - "$state" "$component" > "$patch" <<'PY'
import json,sys
j=json.load(open(sys.argv[1]))
a=json.loads(j['metadata']['annotations']['kubectl.kubernetes.io/last-applied-configuration'])
c=a['spec']['template']['spec']['containers'][0]
x={'name':c['name']}
if sys.argv[2]=='memcached-rate': x['args']=c.get('args')
else:
 x['env']=[{'$patch':'replace'}]+c.get('env',[])
 if sys.argv[2]=='rate': x['resources']={'$patch':'replace',**c.get('resources',{})}
ann=a['spec']['template'].get('metadata',{}).get('annotations',{}).get('kubectl.kubernetes.io/restartedAt')
print(json.dumps({'spec':{'template':{'metadata':{'annotations':{'kubectl.kubernetes.io/restartedAt':ann}},'spec':{'containers':[x]}}}}))
PY
 kubectl patch -n "$NAMESPACE" "deploy/$component" --type=strategic --patch-file "$patch"
 kubectl rollout status -n "$NAMESPACE" "deploy/$component" --timeout=600s
done

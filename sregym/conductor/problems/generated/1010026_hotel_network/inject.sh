#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-hotel-reservation}
ARTIFACT_DIR=${ARTIFACT_DIR:-$(cd "$(dirname "$0")" && pwd)}
mkdir -p "$ARTIFACT_DIR/state-search"
export NAMESPACE ARTIFACT_DIR
kubectl get deployment search -n "$NAMESPACE" -o json > "$ARTIFACT_DIR/state-search/current.json"
python3 - <<'PYCODE'
import json,os,pathlib
s=pathlib.Path(os.environ['ARTIFACT_DIR'])/'state-search'
d=json.loads((s/'current.json').read_text())
b=s/'original.json'
if not b.exists() or json.loads(b.read_text())['metadata']['uid']!=d['metadata']['uid']:
    b.write_text(json.dumps(d))
    (s/'control.go').unlink(missing_ok=True)
    (s/'dialer.go').unlink(missing_ok=True)
    (s/'http2_client.go').unlink(missing_ok=True)
PYCODE
if [[ ! -f "$ARTIFACT_DIR/state-search/control.go" ]]; then
  kubectl exec -n "$NAMESPACE" deployment/search -- cat vendor/google.golang.org/grpc/transport/control.go > "$ARTIFACT_DIR/state-search/control.go"
  kubectl exec -n "$NAMESPACE" deployment/search -- cat dialer/dialer.go > "$ARTIFACT_DIR/state-search/dialer.go"
  kubectl exec -n "$NAMESPACE" deployment/search -- cat vendor/google.golang.org/grpc/transport/http2_client.go > "$ARTIFACT_DIR/state-search/http2_client.go"
fi
python3 - <<'PYCODE'
import json,os,pathlib,hashlib
s=pathlib.Path(os.environ['ARTIFACT_DIR'])/'state-search'
control=(s/'control.go').read_text()
client=(s/'http2_client.go').read_text()
client='\n'.join(line for line in client.split('\n') if 'TODO(mmukhi): Benchmark' not in line)
dialer=(s/'dialer.go').read_text()
assert 'if f.pendingUpdate >= f.limit/4 {' in control
control=control.replace('limit uint32', 'limit uint32\n    updateThreshold uint32',1)
control=control.replace('if f.pendingUpdate >= f.limit/4 {', 'threshold := f.updateThreshold\n    if threshold == 0 { threshold = f.limit/4 }\n    if f.pendingUpdate >= threshold {',1)
assert 'fc:                &inFlow{limit: uint32(icwz)},' in client
client=client.replace('fc:                &inFlow{limit: uint32(icwz)},','fc:                &inFlow{limit: uint32(icwz), updateThreshold: uint32(icwz)/4},',1)
client=client.replace('if opts.InitialWindowSize >= defaultWindowSize {', 'if opts.InitialWindowSize >= defaultWindowSize {\n        t.fc.updateThreshold = uint32(icwz)/4',1)
assert 'for _, fn := range opts {' in dialer
dialer=dialer.replace('for _, fn := range opts {','if name == "srv-rate" {\n        dialopts = append(dialopts, grpc.WithInitialWindowSize(64 * 1024), grpc.WithInitialConnWindowSize(64 * 1024))\n    }\n    for _, fn := range opts {',1)
cm={'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'search-runtime','namespace':os.environ['NAMESPACE']},'data':{'control.go':control,'dialer.go':dialer,'http2_client.go':client}}
(s/'runtime.json').write_text(json.dumps(cm))
d=json.loads((s/'original.json').read_text())
t=d['spec']['template']
p=t['spec']
p.setdefault('volumes',[]).append({'name':'search-runtime','configMap':{'name':'search-runtime'}})
c=p['containers'][0]
c.setdefault('volumeMounts',[]).extend([
 {'name':'search-runtime','mountPath':'/go/src/github.com/harlow/go-micro-services/vendor/google.golang.org/grpc/transport/control.go','subPath':'control.go'},
 {'name':'search-runtime','mountPath':'/go/src/github.com/harlow/go-micro-services/dialer/dialer.go','subPath':'dialer.go'},
 {'name':'search-runtime','mountPath':'/go/src/github.com/harlow/go-micro-services/vendor/google.golang.org/grpc/transport/http2_client.go','subPath':'http2_client.go'}])
c['command']=['/bin/sh','-ec','go build -mod=vendor -o /tmp/search ./cmd/search; exec /tmp/search']
c['readinessProbe']={'tcpSocket':{'port':8082},'periodSeconds':2,'failureThreshold':3}
t['metadata'].setdefault('annotations',{})['checksum/config']=hashlib.sha256(json.dumps(cm['data'],sort_keys=True).encode()).hexdigest()
(s/'prepared.json').write_text(json.dumps({'spec':{'template':t}}))
client=client.replace('t.fc.updateThreshold = uint32(icwz)/4','t.fc.updateThreshold = uint32(icwz)*4',1)
cm['data']['http2_client.go']=client
(s/'runtime-current.json').write_text(json.dumps(cm))
t['metadata']['annotations']['checksum/config']=hashlib.sha256(json.dumps(cm['data'],sort_keys=True).encode()).hexdigest()
(s/'updated.json').write_text(json.dumps({'spec':{'template':t}}))
PYCODE
if ! kubectl get deployment search -n "$NAMESPACE" -o jsonpath='{.spec.template.spec.volumes[*].name}' | python3 -c 'import sys; sys.exit(0 if "search-runtime" in sys.stdin.read().split() else 1)'; then
  kubectl apply -f "$ARTIFACT_DIR/state-search/runtime.json"
  kubectl patch deployment search -n "$NAMESPACE" --type=merge --patch-file "$ARTIFACT_DIR/state-search/prepared.json"
  kubectl rollout status deployment/search -n "$NAMESPACE" --timeout=300s
fi
kubectl apply -f "$ARTIFACT_DIR/state-search/runtime-current.json"
kubectl patch deployment search -n "$NAMESPACE" --type=merge --patch-file "$ARTIFACT_DIR/state-search/updated.json"
kubectl rollout status deployment/search -n "$NAMESPACE" --timeout=300s

# Registrations have no TTL in this application; retain only live pod endpoints.
workdir=$(mktemp -d)
trap 'rm -rf "$workdir"' EXIT
kubectl get pods -n "$NAMESPACE" -l io.kompose.service=search -o json > "$workdir/pods.json"
kubectl exec -n "$NAMESPACE" deployment/consul -- wget -qO- http://127.0.0.1:8500/v1/agent/services > "$workdir/services.json"
python3 - "$workdir/pods.json" "$workdir/services.json" > "$workdir/retired.txt" <<'PYCODE'
import json,sys
pods=json.load(open(sys.argv[1]))['items']
addresses={p['status'].get('podIP') for p in pods if not p['metadata'].get('deletionTimestamp') and p['status'].get('phase')=='Running'}
services=json.load(open(sys.argv[2]))
for sid,s in services.items():
    if s['Service']=='srv-search' and s['Address'] not in addresses:
        print(sid)
PYCODE
while IFS= read -r service_id; do
  kubectl exec -n "$NAMESPACE" deployment/consul -- consul services deregister -id="$service_id"
done < "$workdir/retired.txt"

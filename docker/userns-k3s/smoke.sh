#!/bin/sh
# Smoke test for the userns k3s prototype. Run with a kubeconfig for the cluster.
# Needs nginx:1.27-alpine, busybox:1.36 and nicolaka/netshoot:latest in the air-gap tarball.
set -u
fail=0
check() { if [ "$2" = "$3" ]; then echo "PASS  $1"; else echo "FAIL  $1 (got '$2', want '$3')"; fail=1; fi; }
k() { kubectl -n smoke "$@"; }

kubectl create namespace smoke >/dev/null
k apply -f - >/dev/null <<'YAML'
apiVersion: apps/v1
kind: Deployment
metadata: {name: web}
spec:
  replicas: 3
  selector: {matchLabels: {app: web}}
  template:
    metadata: {labels: {app: web}}
    spec:
      topologySpreadConstraints:
      - {maxSkew: 1, topologyKey: kubernetes.io/hostname, whenUnsatisfiable: DoNotSchedule, labelSelector: {matchLabels: {app: web}}}
      containers:
      - {name: nginx, image: "nginx:1.27-alpine", imagePullPolicy: Never, resources: {limits: {cpu: 200m, memory: 64Mi}}}
---
apiVersion: v1
kind: Service
metadata: {name: web}
spec: {selector: {app: web}, ports: [{port: 80}]}
---
apiVersion: v1
kind: Pod
metadata: {name: client}
spec:
  containers: [{name: c, image: "nicolaka/netshoot:latest", imagePullPolicy: Never, command: ["sleep", "infinity"]}]
---
apiVersion: v1
kind: Pod
metadata: {name: oom}
spec:
  restartPolicy: Never
  containers:
  - {name: hog, image: "busybox:1.36", imagePullPolicy: Never, command: ["sh", "-c", "dd if=/dev/zero of=/dev/null bs=200M count=1"], resources: {limits: {memory: 32Mi}}}
YAML
k wait --for=condition=Ready pod -l app=web --timeout=180s >/dev/null
k wait --for=condition=Ready pod/client --timeout=180s >/dev/null

check "web pods spread over 3 nodes" "$(k get pod -l app=web -o jsonpath='{range .items[*]}{.spec.nodeName}{"\n"}{end}' | sort -u | wc -l | tr -d ' ')" 3
ok=0
for ip in $(k get pod -l app=web -o jsonpath='{.items[*].status.podIP}'); do
    [ "$(k exec client -- curl -s -m 5 -o /dev/null -w '%{http_code}' http://$ip/)" = 200 ] && ok=$((ok+1))
done
check "pod-to-pod across nodes" "$ok" 3
check "Service via cluster DNS" "$(k exec client -- curl -s -m 5 -o /dev/null -w '%{http_code}' http://web.smoke.svc.cluster.local/)" 200
check "no egress to the internet" "$(k exec client -- curl -s -m 5 -o /dev/null -w '%{http_code}' https://example.com 2>/dev/null)" 000

k apply -f - >/dev/null <<'YAML'
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: deny-web}
spec: {podSelector: {matchLabels: {app: web}}, policyTypes: [Ingress]}
YAML
sleep 5
check "NetworkPolicy blocks traffic" "$(k exec client -- curl -s -m 4 -o /dev/null -w '%{http_code}' http://web/ 2>/dev/null)" 000
k delete networkpolicy deny-web >/dev/null
sleep 5
check "traffic restored after policy removal" "$(k exec client -- curl -s -m 5 -o /dev/null -w '%{http_code}' http://web/)" 200

k wait --for=jsonpath='{.status.phase}'=Failed pod/oom --timeout=60s >/dev/null
check "memory limit OOM-kills" "$(k get pod oom -o jsonpath='{.status.containerStatuses[0].state.terminated.reason}')" OOMKilled

# Chaos Mesh style: a privileged hostPID pod on the target's node enters the target
# pod's network namespace and shapes it. (tbf, not netem, so this also works on
# kernels built without sch_netem.)
target=$(k get pod -l app=web -o jsonpath='{.items[0].metadata.name}')
target_ip=$(k get pod "$target" -o jsonpath='{.status.podIP}')
target_node=$(k get pod "$target" -o jsonpath='{.spec.nodeName}')
k apply -f - >/dev/null <<YAML
apiVersion: v1
kind: Pod
metadata: {name: chaosd}
spec:
  nodeName: $target_node
  hostPID: true
  containers:
  - {name: c, image: "nicolaka/netshoot:latest", imagePullPolicy: Never, command: ["sleep", "infinity"], securityContext: {privileged: true}}
YAML
k wait --for=condition=Ready pod/chaosd --timeout=120s >/dev/null
check "privileged hostPID pod runs" "$(k get pod chaosd -o jsonpath='{.status.phase}')" Running
k exec chaosd -- sh -c "for p in \$(pgrep -f 'nginx: master'); do
    nsenter -t \$p -n ip -4 addr show eth0 | grep -q 'inet $target_ip/' &&
    nsenter -t \$p -n tc qdisc add dev eth0 root tbf rate 64kbit burst 4kb latency 500ms && exit 0
done; exit 1"
check "tc qdisc added in target pod netns" "$?" 0
k exec "$target" -- sh -c 'head -c 48000 /dev/urandom > /usr/share/nginx/html/blob'
slow=$(k exec client -- curl -s -m 30 -o /dev/null -w '%{time_total}' "http://$target_ip/blob" | cut -d. -f1)
check "shaped pod is slow (>=3s for 48KB)" "$([ "${slow:-0}" -ge 3 ] && echo yes || echo "no (${slow}s)")" yes

kubectl delete namespace smoke --wait=false >/dev/null
exit $fail

#!/usr/bin/env bash
# Bring up the mini cluster on this host:
#   KWOK control plane (etcd, apiserver, controller-manager, scheduler,
#   kwok-controller as plain processes) + kube-proxy + procnode + CoreDNS.
#
# Requires root, docker (used only as an image store), iproute2, iptables,
# kwokctl, a kwok binary carrying the KWOK_EXEC_PREFIX patch, and a Python
# with the `kubernetes` package.
#
# usage: up.sh [cluster-name]
set -euo pipefail

NAME=${1:-mini}
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
STATE=${STATE:-/var/lib/procnode-cluster/$NAME}
KWOK_BIN=${KWOK_BIN:?path to patched kwok binary}
PYTHON=${PYTHON:-python3}
NODE_IP=${NODE_IP:-$(ip -4 route get 1.1.1.1 | awk '{for (i=1;i<NF;i++) if ($i=="src") print $(i+1)}')}
POD_CIDR=${POD_CIDR:-10.88.0.0/16}
K8S_VERSION=${K8S_VERSION:-}
mkdir -p "$STATE"
KCFG=$HOME/.kwok/clusters/$NAME/kubeconfig.yaml

echo "==> control plane"
kwokctl create cluster --name "$NAME" --runtime binary --kubeconfig "$STATE/kubeconfig" \
  --kwok-controller-binary "$KWOK_BIN" --enable-crds=Logs,Exec,PortForward \
  --config "$HERE/kwok.yaml" --wait 3m
K="kubectl --kubeconfig $KCFG"
VERSION=$($K version -o json | $PYTHON -c 'import json,sys; print(json.load(sys.stdin)["serverVersion"]["gitVersion"])')

echo "==> nodes and storage"
# The apiserver reaches each "kubelet" (kwok-controller) by node hostname.
grep -q "kind-control-plane" /etc/hosts || echo "$NODE_IP kind-control-plane kind-worker kind-worker2 kind-worker3" >> /etc/hosts
$K apply -f "$HERE/nodes.yaml"

echo "==> host networking"
echo 1 > /proc/sys/net/ipv4/ip_forward
iptables -C FORWARD -s "$POD_CIDR" -j ACCEPT 2>/dev/null || iptables -I FORWARD 1 -s "$POD_CIDR" -j ACCEPT
iptables -C FORWARD -d "$POD_CIDR" -j ACCEPT 2>/dev/null || iptables -I FORWARD 1 -d "$POD_CIDR" -j ACCEPT
iptables -t nat -C POSTROUTING -s "$POD_CIDR" ! -d "$POD_CIDR" -j MASQUERADE 2>/dev/null ||
  iptables -t nat -A POSTROUTING -s "$POD_CIDR" ! -d "$POD_CIDR" -j MASQUERADE

echo "==> kube-proxy ($VERSION)"
if [[ ! -x $STATE/kube-proxy ]]; then
  curl -sSLo "$STATE/kube-proxy" "https://dl.k8s.io/release/$VERSION/bin/linux/amd64/kube-proxy"
  chmod +x "$STATE/kube-proxy"
fi
setsid nohup "$STATE/kube-proxy" --kubeconfig "$KCFG" --hostname-override kind-worker \
  --cluster-cidr "$POD_CIDR" --proxy-mode iptables \
  --metrics-bind-address 127.0.0.1:10249 --healthz-bind-address 127.0.0.1:10256 \
  > "$STATE/kube-proxy.log" 2>&1 < /dev/null &
echo $! > "$STATE/kube-proxy.pid"

echo "==> procnode"
setsid nohup "$PYTHON" "$HERE/../procnode/procnode.py" --kubeconfig "$KCFG" --node-ip "$NODE_IP" \
  --primary-node kind-worker --pod-cidr "$POD_CIDR" --dns-ip 10.96.0.10 \
  > "$STATE/procnode.log" 2>&1 < /dev/null &
echo $! > "$STATE/procnode.pid"

echo "==> CoreDNS"
$K apply -f "$HERE/coredns.yaml"
$K -n kube-system rollout status deploy/coredns --timeout=180s

echo "ready: export KUBECONFIG=$KCFG"

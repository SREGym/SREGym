#!/bin/bash
# Bring up one Harbor trial inside this container, as root: an image-pull proxy,
# the unprivileged four-node cluster (docker/userns-k3s), then the SREGym
# backend, which deploys the problem, injects its fault and grades it later.
# sregym-ready starts this once. A failure before the backend runs is written to
# the shared state, so the healthcheck stops at once.
set -euo pipefail
shared=/run/sregym
out=/sregym-harbor
kubeconfig=/var/lib/rancher/k3s/nodes/kind-control-plane/etc-rancher/k3s/k3s.yaml
install -d -m 755 "$shared"
install -d -m 700 "$out" "$out/logs" /var/lib/rancher /root/.kube
exec >>"$out/logs/start.log" 2>&1

stage="startup"
fail() {
    local reason="SREGym setup failed during: $stage. Log: $out/logs/start.log"
    echo "$reason"
    printf '{"state": "failed", "error": "%s"}\n' "$reason" > "$shared/status.json"
    echo failed > "$shared/state"
}
trap fail ERR

stage="image-pull proxy"
python3 /usr/local/lib/userns-k3s/egress-proxy.py --listen 127.0.0.1 --port 3128 >"$out/logs/egress.log" 2>&1 &

stage="cluster"
EGRESS_PROXY=127.0.0.1:3128 /bin/sh /usr/local/lib/userns-k3s/entrypoint.sh >"$out/logs/cluster.log" 2>&1 &
timeout 300 bash -c "until [ -s $kubeconfig ]; do sleep 2; done"
cp "$kubeconfig" /root/.kube/config
export KUBECONFIG=/root/.kube/config
timeout 300 bash -c 'until [ "$(kubectl get nodes --no-headers 2>/dev/null | grep -cw Ready)" -ge 4 ]; do sleep 3; done'
# SREGym's charts select KIND's empty control-plane label, which node.sh sets.
timeout 120 bash -c 'until [ -z "$(kubectl get node kind-control-plane -o jsonpath="{.metadata.labels.node-role\.kubernetes\.io/control-plane}")" ]; do sleep 2; done'
kubectl get nodes

stage="SREGym backend"
trap - ERR
cd /opt/sregym
# Social Network's ID services read eth0's MAC from sysfs, which pods cannot
# read under Sysbox; its chart then mounts this one instead. Same on every host.
export SREGYM_FIXED_MAC_ADDRESS=02:42:ac:11:00:02
SREGYM_PROBLEM_ID=$(cat /etc/sregym/problem) \
SREGYM_ORACLE_TOKEN_SHA256=$(cat /etc/sregym/oracle-token-sha256) \
SREGYM_STEADY_STATE_S=$(cat /etc/sregym/steady-state-seconds 2>/dev/null || echo 0) \
    exec python -m sregym.harbor.backend --advertise-host 127.0.0.1 --shared-dir "$shared" --output-dir "$out"

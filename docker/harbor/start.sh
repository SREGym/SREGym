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
    local reason="${1:-SREGym setup failed during: $stage. Log: $out/logs/start.log}"
    echo "$reason"
    printf '{"state": "failed", "error": "%s"}\n' "$reason" > "$shared/status.json"
    echo failed > "$shared/state"
}
trap fail ERR

# The kernel charges inotify instances to a user namespace's owner, against the
# host's per-user limit (fs.inotify.max_user_instances, 128 by default), which a
# container cannot raise. Given CAP_SYS_ADMIN, the fabric gives each node its own
# namespace and owner: the busiest node peaked at 47 (Astronomy Shop). Otherwise
# the whole cluster shares one owner and peaked at 177.
stage="host check"
cap_eff=$(awk '/^CapEff:/ {print $2}' /proc/self/status)
if grep -qE '^ +0 +0 +4294967295$' /proc/self/uid_map && [ $(( (0x$cap_eff >> 21) & 1 )) -eq 1 ]; then
    inotify_needed=96
else
    inotify_needed=256
fi
inotify_limit=$(cat /proc/sys/fs/inotify/max_user_instances 2>/dev/null || echo 0)
echo "inotify: max_user_instances=$inotify_limit (needs $inotify_needed) max_user_watches=$(cat /proc/sys/fs/inotify/max_user_watches 2>/dev/null)"
if [ "$inotify_limit" -lt "$inotify_needed" ]; then
    trap - ERR
    fail "This Docker host allows $inotify_limit inotify instances per user (fs.inotify.max_user_instances), and SREGym's Kubernetes cluster needs about $inotify_needed. Run it on a host with a higher limit."
    exit 1
fi

# Optional image-only bundle. Copy it once onto the fresh cluster volume, where
# the nodes can hard-link archives rather than each copying them from overlayfs.
# No Kubernetes databases, credentials, snapshots or oracle state are restored.
if [ -d /opt/sregym-preload ]; then
    stage="preloaded images"
    bundle_arch=$(python3 -c 'import json; print(json.load(open("/opt/sregym-preload/bundle.json"))["architecture"])')
    case $(uname -m) in
        x86_64) runtime_arch=amd64 ;;
        aarch64|arm64) runtime_arch=arm64 ;;
        *) runtime_arch=unsupported ;;
    esac
    if [ "$bundle_arch" != "$runtime_arch" ]; then
        echo "Image bundle architecture $bundle_arch does not match runtime $runtime_arch" >&2
        false
    fi
    install -d -m 700 /var/lib/rancher/k3s/airgap/workers
    for role in common workers; do
        source=/opt/sregym-preload/$role.tar
        [ -f "$source" ] || continue
        target=/var/lib/rancher/k3s/airgap/$role.tar
        [ "$role" = workers ] && target=/var/lib/rancher/k3s/airgap/workers/workers.tar
        echo "Staging $role image archive on the fresh cluster volume"
        cp "$source" "$target"
    done
fi

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

if [ -d /opt/sregym-preload ]; then
    stage="preloaded image references"
    python3 /usr/local/lib/userns-k3s/preload-aliases.py wait
fi

stage="SREGym backend"
trap - ERR
cd /opt/sregym
# Social Network's ID services read eth0's MAC from sysfs, which pods cannot
# read under Sysbox; its chart then mounts this one instead. Same on every host.
export SREGYM_FIXED_MAC_ADDRESS=02:42:ac:11:00:02
SREGYM_PROBLEM_ID=$(cat /etc/sregym/problem) \
SREGYM_STEADY_STATE_S=$(cat /etc/sregym/steady-state-seconds 2>/dev/null || echo 0) \
    exec python -u -m sregym.harbor.backend --advertise-host 127.0.0.1 --shared-dir "$shared" --output-dir "$out"

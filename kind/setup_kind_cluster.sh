#!/usr/bin/env bash
# setup_kind_cluster.sh
# Creates a Kind cluster with Calico CNI for SREGym.
#
# Usage (from repo root):
#   bash kind/setup_kind_cluster.sh [arm|x86]
#
# Requirements:
#   - kind
#   - kubectl

set -euo pipefail

CALICO_VERSION="v3.27.0"
ARCH="${1:-x86}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIND_CONFIG="${KIND_CONFIG:-${SCRIPT_DIR}/kind-config-${ARCH}.yaml}"

if [[ ! -f "${KIND_CONFIG}" ]]; then
    echo "❌ Config file not found: ${KIND_CONFIG}"
    echo "Usage: bash kind/setup_kind_cluster.sh [arm|x86]"
    exit 1
fi

echo "==> Step 1: Create Kind cluster (arch: ${ARCH})"
CREATE_ARGS=(--config "${KIND_CONFIG}")
if [[ -n ${KIND_NODE_IMAGE:-} ]]; then
    CREATE_ARGS+=(--image "${KIND_NODE_IMAGE}")
fi
if [[ ${KIND_RETAIN_ON_FAILURE:-false} == true ]]; then
    CREATE_ARGS+=(--retain)
fi
kind create cluster "${CREATE_ARGS[@]}"

echo "==> Step 2: Install Calico CNI"
# Cold parallel starts can briefly overload etcd while node images unpack.
# Apply is declarative, so retrying also handles a partially applied manifest.
for attempt in 1 2 3; do
    if kubectl apply -f "https://raw.githubusercontent.com/projectcalico/calico/${CALICO_VERSION}/manifests/calico.yaml"; then
        break
    fi
    if [[ $attempt == 3 ]]; then
        echo "Calico installation failed after ${attempt} attempts." >&2
        exit 1
    fi
    sleep 5
done

echo "==> Step 3: Wait for Calico to be ready"
# 300s (vs 120s) accommodates first-time image pulls on slow CI runners,
# where 1 of N calico-node pods routinely lags behind the others.
CALICO_TIMEOUT="${CALICO_TIMEOUT:-300s}"

dump_calico_diagnostics() {
    echo ""
    echo "❌ Calico did not reach Ready within ${CALICO_TIMEOUT}. Dumping diagnostics:"
    echo "--- nodes ---"
    kubectl get nodes -o wide || true
    echo "--- calico pods ---"
    kubectl -n kube-system get pods -l k8s-app=calico-node -o wide || true
    echo "--- describe unready calico pods ---"
    kubectl -n kube-system get pods -l k8s-app=calico-node \
        --field-selector=status.phase!=Running -o name 2>/dev/null \
        | xargs -r -I{} kubectl -n kube-system describe {} || true
    echo "--- recent kube-system events ---"
    kubectl -n kube-system get events --sort-by='.lastTimestamp' | tail -40 || true
}

if ! kubectl rollout status daemonset/calico-node -n kube-system --timeout="${CALICO_TIMEOUT}"; then
    dump_calico_diagnostics
    exit 1
fi
if ! kubectl wait --for=condition=ready pod -l k8s-app=calico-node -n kube-system --timeout="${CALICO_TIMEOUT}"; then
    dump_calico_diagnostics
    exit 1
fi

echo "==> Step 3b: Confirm all nodes are Ready"
# Calico rollout completing does not on its own guarantee nodes flip to Ready
# (kubelet has its own debounce). Assert it explicitly so a stuck node fails
# loudly here, not 10 minutes later inside wait_for_ready('kube-system').
if ! kubectl wait --for=condition=Ready nodes --all --timeout=120s; then
    echo "❌ Nodes did not reach Ready after Calico install."
    kubectl get nodes -o wide || true
    kubectl describe nodes || true
    exit 1
fi

echo "==> Step 4: Delete SREGym cluster baseline cache"
# SREGym caches the cluster baseline state after first deployment.
# Deleting it forces SREGym to capture a fresh baseline with Calico installed.
rm -f ~/cache_dir/cluster_baseline_state.json

echo ""
echo "✅ Cluster setup complete!"
echo ""

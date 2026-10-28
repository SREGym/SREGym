#!/usr/bin/env bash
# setup_kind_cluster.sh
# Creates a Kind cluster with Calico CNI for SREGym.
#
# Usage (from repo root):
#   bash kind/setup_kind_cluster.sh [auto|arm|x86]
#
# Environment:
#   KIND_CLUSTER_NAME  Name of the cluster to create. Unset, kind uses its
#                      default ("kind"). Two clusters on one host need
#                      distinct names, or the second create fails. The name
#                      is passed to every kind command here, since kind
#                      otherwise addresses the default cluster.
#
# Requirements:
#   - kind
#   - kubectl

set -euo pipefail

# Filtered agent runs require Calico policy tiers (available since 3.29).
CALICO_VERSION="v3.29.3"
case "${1:-auto}" in
    auto|arm|x86) ;; # Legacy aliases; the Docker daemon selects the native platform.
    *)
        echo "Usage: bash kind/setup_kind_cluster.sh [auto|arm|x86]"
        exit 1
        ;;
esac
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIND_CONFIG="${KIND_CONFIG:-${SCRIPT_DIR}/kind-config.yaml}"

if [[ ! -f "${KIND_CONFIG}" ]]; then
    echo "❌ Config file not found: ${KIND_CONFIG}"
    echo "Usage: bash kind/setup_kind_cluster.sh [auto|arm|x86]"
    exit 1
fi

BASELINE_CACHE_PATH="${SREGYM_BASELINE_CACHE_PATH:-${SREGYM_CACHE_DIR:-${HOME}/cache_dir}/cluster_baseline_state.json}"

echo "==> Step 1: Invalidate cached cluster baseline"
# A newly created Kind cluster has different resource identities. Invalidate
# before creation so a later setup failure cannot leave an old baseline eligible
# for the next benchmark attempt.
rm -f "${BASELINE_CACHE_PATH}"

echo "==> Step 2: Create Kind cluster"
CREATE_ARGS=(--config "${KIND_CONFIG}")
if [[ -n ${KIND_NODE_IMAGE:-} ]]; then
    CREATE_ARGS+=(--image "${KIND_NODE_IMAGE}")
fi
if [[ ${KIND_RETAIN_ON_FAILURE:-false} == true ]]; then
    CREATE_ARGS+=(--retain)
fi
if [[ -n ${KIND_CLUSTER_NAME:-} ]]; then
    CREATE_ARGS+=(--name "${KIND_CLUSTER_NAME}")
fi
kind create cluster "${CREATE_ARGS[@]}"

echo "==> Step 2b: Drop host DNS search domains from the nodes"
# Docker copies the host's "search" list into each node's /etc/resolv.conf and
# kubelet appends it to every pod's search list. With ndots:5 a short name such
# as "frontend" whose AAAA lookup has no cluster answer then walks the host
# domains. On hosts whose search domains contain a real "frontend" (for example
# corporate networks) Envoy in astronomy-shop's frontend-proxy resolves that
# foreign address and every request fails with 503. Pods only need the cluster
# search domains, which kubelet adds on its own.
CONTAINER_CLI="${KIND_EXPERIMENTAL_PROVIDER:-docker}"
# shellcheck disable=SC2086 # the expansion is empty or exactly two words
for node in $(kind get nodes ${KIND_CLUSTER_NAME:+--name "${KIND_CLUSTER_NAME}"} 2>/dev/null); do
    # /etc/resolv.conf is a bind mount: rewrite the content, not the inode.
    "${CONTAINER_CLI}" exec "${node}" sh -c \
        'awk "!/^search /" /etc/resolv.conf > /tmp/resolv.conf.sregym && cat /tmp/resolv.conf.sregym > /etc/resolv.conf'
done

echo "==> Step 3: Install Calico CNI"
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

echo "==> Step 4: Wait for Calico to be ready"
# A fresh four-node cluster pulls the CNI and node images into four independent
# containerd stores. Slow registries can take more than five minutes without the
# rollout being unhealthy, so keep this aligned with the cold-start budget used
# by the benchmark runtime.
CALICO_TIMEOUT="${CALICO_TIMEOUT:-900s}"

dump_calico_diagnostics() {
    echo ""
    echo "❌ Calico did not reach Ready within ${CALICO_TIMEOUT}. Dumping diagnostics:"
    echo "--- nodes ---"
    kubectl get nodes -o wide || true
    echo "--- calico pods ---"
    kubectl -n kube-system get pods -l k8s-app=calico-node -o wide || true
    echo "--- describe unready calico pods ---"
    while IFS= read -r pod; do
        [[ -n "${pod}" ]] && kubectl -n kube-system describe "${pod}" || true
    done < <(
        kubectl -n kube-system get pods -l k8s-app=calico-node \
            --field-selector=status.phase!=Running -o name 2>/dev/null || true
    )
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

echo "==> Step 4b: Confirm all nodes are Ready"
# Calico rollout completing does not on its own guarantee nodes flip to Ready
# (kubelet has its own debounce). Assert it explicitly so a stuck node fails
# loudly here, not 10 minutes later inside wait_for_ready('kube-system').
if ! kubectl wait --for=condition=Ready nodes --all --timeout=120s; then
    echo "❌ Nodes did not reach Ready after Calico install."
    kubectl get nodes -o wide || true
    kubectl describe nodes || true
    exit 1
fi

echo "==> Step 4c: Confirm pod sandbox creation on every Linux node"
# Ready nodes and Calico pods do not prove that each node's CNI credentials
# work. Create all probes before waiting so one healthy worker cannot hide a
# broken worker, without serializing image pulls across nodes.
NETWORK_PROBE_NAMESPACE="${NETWORK_PROBE_NAMESPACE:-kube-system}"
NETWORK_PROBE_IMAGE="${SREGYM_NETWORK_PROBE_IMAGE:-${NETWORK_PROBE_IMAGE:-registry.k8s.io/pause:3.10}}"
NETWORK_PROBE_TIMEOUT="${NETWORK_PROBE_TIMEOUT:-120s}"
NETWORK_PROBE_RUN="sregym-cni-probe-${RANDOM}-${RANDOM}"
NETWORK_PROBE_NAMES=()

cleanup_network_probes() {
    if [[ ${#NETWORK_PROBE_NAMES[@]} -gt 0 ]]; then
        if ! kubectl delete pod "${NETWORK_PROBE_NAMES[@]}" --namespace="${NETWORK_PROBE_NAMESPACE}" \
            --ignore-not-found --grace-period=0 --force --wait=false --request-timeout=10s >/dev/null 2>&1; then
            echo "Could not remove network probe pods: ${NETWORK_PROBE_NAMES[*]}" >&2
            return 1
        fi
    fi
}
trap 'cleanup_network_probes || true' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

NETWORK_PROBE_NODES=$(kubectl get nodes --selector=kubernetes.io/os=linux \
    --field-selector=spec.unschedulable=false \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' --request-timeout=10s)
if [[ -z "${NETWORK_PROBE_NODES}" ]]; then
    echo "❌ Cannot check pod networking: no uncordoned Linux nodes."
    exit 1
fi

for node in ${NETWORK_PROBE_NODES}; do
    name="${NETWORK_PROBE_RUN}-${#NETWORK_PROBE_NAMES[@]}"
    NETWORK_PROBE_NAMES+=("${name}")
    printf -v overrides \
        '{"spec":{"nodeName":"%s","automountServiceAccountToken":false,"tolerations":[{"operator":"Exists"}]}}' \
        "${node}"
    if ! kubectl run "${name}" --namespace="${NETWORK_PROBE_NAMESPACE}" \
        --image="${NETWORK_PROBE_IMAGE}" --image-pull-policy=IfNotPresent --restart=Never \
        --overrides="${overrides}" --labels="app=sregym-cni-probe,sregym-probe-run=${NETWORK_PROBE_RUN}" \
        --request-timeout=10s; then
        echo "❌ Could not create a network probe on ${node}."
        exit 1
    fi
done

for name in "${NETWORK_PROBE_NAMES[@]}"; do
    if ! kubectl wait --for=condition=Ready "pod/${name}" \
        --namespace="${NETWORK_PROBE_NAMESPACE}" --timeout="${NETWORK_PROBE_TIMEOUT}"; then
        echo "❌ Pod sandbox probe ${name} did not become Ready within ${NETWORK_PROBE_TIMEOUT}."
        echo "--- network probe pods ---"
        kubectl get pods --namespace="${NETWORK_PROBE_NAMESPACE}" \
            -l "sregym-probe-run=${NETWORK_PROBE_RUN}" -o wide --request-timeout=10s || true
        kubectl describe pod "${name}" --namespace="${NETWORK_PROBE_NAMESPACE}" --request-timeout=10s || true
        echo "--- probe events ---"
        kubectl get events --namespace="${NETWORK_PROBE_NAMESPACE}" \
            --field-selector="involvedObject.name=${name}" --sort-by='.lastTimestamp' --request-timeout=10s || true
        exit 1
    fi
done
cleanup_network_probes
trap - EXIT INT TERM

echo ""
echo "✅ Cluster setup complete!"
echo ""

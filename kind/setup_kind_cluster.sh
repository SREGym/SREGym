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
ARCH="${1:-arm}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIND_CONFIG="${SCRIPT_DIR}/kind-config-${ARCH}.yaml"

if [[ ! -f "${KIND_CONFIG}" ]]; then
    echo "❌ Config file not found: ${KIND_CONFIG}"
    echo "Usage: bash kind/setup_kind_cluster.sh [arm|x86]"
    exit 1
fi

echo "==> Step 1: Create Kind cluster (arch: ${ARCH})"
kind create cluster --config "${KIND_CONFIG}"

echo "==> Step 2: Install Calico CNI"
kubectl apply -f "https://raw.githubusercontent.com/projectcalico/calico/${CALICO_VERSION}/manifests/calico.yaml"

echo "==> Step 3: Wait for Calico to be ready"
kubectl rollout status daemonset/calico-node -n kube-system --timeout=120s
kubectl wait --for=condition=ready pod -l k8s-app=calico-node -n kube-system --timeout=120s

echo "==> Step 4: Delete SREGym cluster baseline cache"
# SREGym caches the cluster baseline state after first deployment.
# Deleting it forces SREGym to capture a fresh baseline with Calico installed.
rm -f ~/cache_dir/cluster_baseline_state.json

echo ""
echo "✅ Cluster setup complete!"
echo ""

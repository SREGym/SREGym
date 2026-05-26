#!/usr/bin/env bash
# setup_calico_cluster.sh
# Sets up a Kind cluster with Calico CNI for the pod_cidr_exhaustion_hotel_reservation
# benchmark problem.
#
# Usage (from repo root):
#   bash kind/setup_calico_cluster.sh [arm|x86]
#
# Requirements:
#   - kind
#   - kubectl

set -euo pipefail

CALICO_VERSION="v3.27.0"
CLUSTER_NAME="srelab"
ARCH="${1:-arm}"

if [[ "${ARCH}" == "arm" ]]; then
    NODE_IMAGE="jacksonarthurclark/aiopslab-kind-arm:latest"
elif [[ "${ARCH}" == "x86" ]]; then
    NODE_IMAGE="jacksonarthurclark/aiopslab-kind-x86:latest"
else
    echo "❌ Unknown arch: ${ARCH}. Use 'arm' or 'x86'."
    exit 1
fi

echo "==> Step 1: Create Kind cluster with disableDefaultCNI (arch: ${ARCH})"
cat <<EOF | kind create cluster --name "${CLUSTER_NAME}" --config=-
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
networking:
  disableDefaultCNI: true
nodes:
  - role: control-plane
    image: ${NODE_IMAGE}
    extraMounts:
      - hostPath: /run/udev
        containerPath: /run/udev
  - role: worker
    image: ${NODE_IMAGE}
    extraMounts:
      - hostPath: /run/udev
        containerPath: /run/udev
  - role: worker
    image: ${NODE_IMAGE}
    extraMounts:
      - hostPath: /run/udev
        containerPath: /run/udev
  - role: worker
    image: ${NODE_IMAGE}
    extraMounts:
      - hostPath: /run/udev
        containerPath: /run/udev
EOF

echo "==> Step 2: Install Calico CNI"
kubectl apply -f "https://raw.githubusercontent.com/projectcalico/calico/${CALICO_VERSION}/manifests/calico.yaml"

echo "==> Step 3: Wait for Calico to be ready"
kubectl rollout status daemonset/calico-node -n kube-system --timeout=120s
kubectl wait --for=condition=ready pod -l k8s-app=calico-node -n kube-system --timeout=120s

echo "==> Step 4: Verify IPAMConfig and IPPool exist"
kubectl get ipamconfig default
kubectl get ippool default-ipv4-ippool

echo "==> Step 5: Delete SREGym cluster baseline cache"
rm -f ~/cache_dir/cluster_baseline_state.json

echo ""
echo "✅ Cluster setup complete!"
echo ""
echo "Notes:"
echo "  - Calico CNI installed (replaces kindnet)"

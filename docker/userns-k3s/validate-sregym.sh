#!/usr/bin/env bash
# Run SREGym's problem lifecycle validator against the unprivileged cluster:
# deploy the app, inject the fault, require the mitigation oracle to fail,
# recover, require it to pass. The cluster runs in one container started
# WITHOUT --privileged, added capabilities or devices.
#
# Usage: docker/userns-k3s/validate-sregym.sh <problem-id>...
#
# Needs docker, uv, kubectl and helm (4+) on PATH, and network access for image
# pulls. Writes ~/.kube/config, which SREGym's API proxy reads, and keeps a
# backup of an existing one. Summaries go to results/userns-k3s/.
#
# Environment:
#   EGRESS_PROXY      host:port of an HTTP proxy that containerd uses for pulls
#   CA_BUNDLE         PEM bundle to trust for pulls (TLS-inspecting proxies)
#   REGISTRIES        containerd registries.yaml (e.g. docker/userns-k3s/registries.yaml)
#   DOCKER_NETWORK    network for the container; "host" reaches a proxy on loopback
#   OPENEBS_MANIFEST  operator manifest URL to pre-apply when openebs.github.io is blocked
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
name=userns-k3s
results=$repo/results/userns-k3s
kubeconfig=/var/lib/rancher/k3s/nodes/kind-control-plane/etc-rancher/k3s/k3s.yaml
[[ $# -gt 0 ]] || { echo "usage: $0 <problem-id>..." >&2; exit 2; }
for tool in docker uv kubectl helm; do
    command -v "$tool" >/dev/null || { echo "$tool is required on PATH" >&2; exit 1; }
done

if [[ $(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null) != true ]]; then
    docker rm -f "$name" >/dev/null 2>&1 || true
    docker build -q -t sregym-userns-k3s "$here" >/dev/null
    args=(--security-opt "seccomp=$here/seccomp.json" --security-opt systempaths=unconfined --cgroupns=private)
    [[ -n ${DOCKER_NETWORK:-} ]] && args+=(--network "$DOCKER_NETWORK")
    [[ ${DOCKER_NETWORK:-} == host ]] || args+=(-p 127.0.0.1:6443:6443)
    [[ -n ${EGRESS_PROXY:-} ]] && args+=(-e "EGRESS_PROXY=$EGRESS_PROXY")
    [[ -n ${CA_BUNDLE:-} ]] && args+=(-v "$CA_BUNDLE:/etc/ssl/certs/ca-certificates.crt:ro")
    [[ -n ${REGISTRIES:-} ]] && args+=(-v "$(realpath "$REGISTRIES"):/etc/userns-k3s/registries.yaml:ro")
    docker run -d --name "$name" "${args[@]}" -v "$name:/var/lib/rancher/k3s" sregym-userns-k3s >/dev/null
fi
docker inspect "$name" --format 'Privileged={{.HostConfig.Privileged}} CapAdd={{.HostConfig.CapAdd}} Devices={{.HostConfig.Devices}}'

timeout 300 bash -c "until docker exec $name test -s $kubeconfig 2>/dev/null; do sleep 3; done"
mkdir -p ~/.kube
if [[ -f ~/.kube/config ]] && ! cmp -s ~/.kube/config <(docker exec "$name" cat "$kubeconfig"); then
    cp -n ~/.kube/config ~/.kube/config.before-userns-k3s
fi
docker exec "$name" cat "$kubeconfig" > ~/.kube/config
chmod 600 ~/.kube/config
timeout 300 bash -c 'until [ "$(kubectl get nodes --no-headers 2>/dev/null | grep -c " Ready")" -ge 4 ]; do sleep 5; done'
timeout 120 bash -c 'until [ -z "$(kubectl get node kind-control-plane -o jsonpath="{.metadata.labels.node-role\.kubernetes\.io/control-plane}")" ]; do sleep 2; done'
kubectl get nodes

if [[ -n ${OPENEBS_MANIFEST:-} ]] && ! kubectl -n openebs get deployment openebs-localpv-provisioner >/dev/null 2>&1; then
    # The same operator manifest and patches SREGym applies, from a reachable URL.
    kubectl apply -f "$OPENEBS_MANIFEST"
    kubectl patch storageclass openebs-hostpath \
        -p '{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}'
    kubectl -n openebs patch ds openebs-ndm --type=json \
        -p '[{"op":"add","path":"/spec/template/spec/nodeSelector","value":{}}]'
fi

mkdir -p "$results"
declare -A outcome
for problem in "$@"; do
    echo "=== $problem"
    if (cd "$repo" && env -u KUBECONFIG uv run --frozen python tests/integration/validate_problem.py \
        --problem "$problem" --summary "$results/$problem.md" > "$results/$problem.log" 2>&1); then
        outcome[$problem]=PASS
    else
        outcome[$problem]=FAIL
    fi
    grep -E "✅|❌|⏭️" "$results/$problem.log" | cut -c1-160 || true
done

echo
failed=0
for problem in "$@"; do
    echo "${outcome[$problem]}  $problem"
    [[ ${outcome[$problem]} == PASS ]] || failed=1
done
exit $failed

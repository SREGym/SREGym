#!/usr/bin/env bash
# Run inside a ready DinD environment. No LLM credentials required.
set -euo pipefail
kubectl wait --for=condition=Ready nodes --all --timeout=120s
[[ $(kind get nodes | wc -l) -eq 4 ]]
[[ $(docker ps --filter label=io.x-k8s.kind.cluster=kind -q | wc -l) -eq 4 ]]

# Validate the nested bind mounts and host networking used by agent containers.
probe=$(mktemp)
server_pid=
trap '[[ -z $server_pid ]] || kill "$server_pid"; rm -f "$probe"; kubectl delete namespace dind-smoke --ignore-not-found --wait=false' EXIT
echo nested-mount-ok > "$probe"
docker run --rm -v "$probe:/probe:ro" alpine:3.21 grep -q nested-mount-ok /probe
python -m http.server 18765 --bind 127.0.0.1 --directory "$(dirname "$probe")" >/dev/null 2>&1 &
server_pid=$!
docker run --rm --network host curlimages/curl:8.12.1 \
    --fail --silent --retry 10 --retry-connrefused "http://127.0.0.1:18765/$(basename "$probe")" \
    | grep -q nested-mount-ok

kubectl create namespace dind-smoke
kubectl -n dind-smoke create deployment web --image=nginx:1.27-alpine
kubectl -n dind-smoke rollout status deployment/web --timeout=300s
kubectl -n dind-smoke expose deployment web --port=80
kubectl -n dind-smoke run client --image=curlimages/curl:8.12.1 \
    --restart=Never --command -- curl --fail --retry 20 --retry-all-errors --retry-delay 2 http://web
kubectl -n dind-smoke wait --for=jsonpath='{.status.phase}'=Succeeded pod/client --timeout=120s
# Exercise a reversible Kubernetes fault and recovery.
kubectl -n dind-smoke scale deployment web --replicas=0
kubectl -n dind-smoke wait --for=delete pod -l app=web --timeout=120s
kubectl -n dind-smoke scale deployment web --replicas=1
kubectl -n dind-smoke rollout status deployment/web --timeout=120s
echo 'DinD smoke test passed'

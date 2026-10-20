#!/usr/bin/env bash
# Tier-0 cost: how much control plane does a *simulated* hotel-reservation
# environment cost when every pod is fake (KWOK's default pod stages)?
#
# Creates a separate kwokctl cluster, applies N copies of the app's manifests
# (one namespace each), waits until every pod is Running and records the RSS
# of etcd/apiserver/controller-manager/scheduler/kwok-controller.
#
# usage: kwok_fake_scale.sh OUT.csv N1 N2 ...   (cumulative: 1 10 50 => 50 total)
set -euo pipefail

OUT=$1; shift
NAME=fake-scale
MANIFESTS=${MANIFESTS:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/SREGym-applications/hotelReservation/kubernetes}
kwokctl delete cluster --name $NAME >/dev/null 2>&1 || true
t0=$(date +%s.%N)
kwokctl create cluster --name $NAME --runtime binary --wait 3m >/dev/null 2>&1
echo "cluster up in $(echo "$(date +%s.%N) - $t0" | bc)s"
export KUBECONFIG=$HOME/.kwok/clusters/$NAME/kubeconfig.yaml
# 110 pods per fake node; leave room for 100+ environments.
kwokctl scale node --name $NAME --replicas "${NODES:-25}" >/dev/null 2>&1

bind_pvcs() {  # a pure-KWOK cluster has no provisioner: pre-bind a PV to each claim
  local ns=$1 pvc
  for pvc in $(kubectl get pvc -n "$ns" -o jsonpath='{.items[*].metadata.name}'); do
    printf '%s\n' "apiVersion: v1" "kind: PersistentVolume" "metadata: {name: $ns-$pvc}" "spec:" \
      "  capacity: {storage: 1Gi}" "  accessModes: [ReadWriteOnce]" "  hostPath: {path: /tmp/$ns-$pvc}" \
      "  claimRef: {namespace: $ns, name: $pvc}" | kubectl create -f - >/dev/null
  done
}
rss() {  # MiB of one component
  local pid; pid=$(cat "$HOME/.kwok/clusters/$NAME/pids/$1.pid")
  awk '/VmRSS/{printf "%.0f", $2/1024}' "/proc/$pid/status"
}
snapshot() {
  sleep 10  # let watch caches settle
  echo "$1,$(kubectl get pods -A --no-headers | wc -l),$(rss etcd),$(rss kube-apiserver),$(rss kube-controller-manager),$(rss kube-scheduler),$(rss kwok-controller),$2" >> "$OUT"
}
echo "envs,pods,etcd_mib,apiserver_mib,kcm_mib,scheduler_mib,kwok_mib,ready_s" > "$OUT"
snapshot 0 0

have=0
for target in "$@"; do
  t0=$(date +%s.%N)
  for ((i = have; i < target; i++)); do
    kubectl create namespace "hr-$i" >/dev/null
    (kubectl apply -n "hr-$i" -Rf "$MANIFESTS" >/dev/null 2>&1; bind_pvcs "hr-$i") &
    (( (i - have) % 10 == 9 )) && wait
  done
  wait
  want=$((target * 19))
  until [[ $(kubectl get pods -A --no-headers --field-selector=status.phase=Running 2>/dev/null | grep -c "^hr-") -ge $want ]]; do sleep 2; done
  have=$target
  snapshot "$target" "$(echo "$(date +%s.%N) - $t0" | bc)"
  tail -1 "$OUT"
done
kwokctl delete cluster --name $NAME >/dev/null 2>&1

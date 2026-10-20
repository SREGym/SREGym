#!/usr/bin/env bash
# What does one isolated control plane (etcd, apiserver, controller-manager,
# scheduler, kwok-controller as processes) cost per environment, and how much
# of it is Go heap slack that GC tuning gives back?
#
# usage: cp_cost.sh OUT.csv [copies]
set -euo pipefail

OUT=$1
COPIES=${2:-3}
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

cat > "$TMP/tuned.yaml" <<'EOF'
apiVersion: config.kwok.x-k8s.io/v1alpha1
kind: KwokctlConfiguration
componentsPatches:
EOF
for c in etcd kube-apiserver kube-controller-manager kube-scheduler kwok-controller; do
  cat >> "$TMP/tuned.yaml" <<EOF
- name: $c
  extraEnvs:
  - {name: GOGC, value: "50"}
  - {name: GOMEMLIMIT, value: "150MiB"}
EOF
done

rss() {  # MiB of one component of one cluster
  awk '/VmRSS/{printf "%.0f", $2/1024}' "/proc/$(cat "$HOME/.kwok/clusters/$1/pids/$2.pid")/status"
}

echo "config,cluster,etcd_mib,apiserver_mib,kcm_mib,scheduler_mib,kwok_mib,total_mib,create_s" > "$OUT"
for cfg in default tuned; do
  for ((i = 0; i < COPIES; i++)); do
    name="cp-$cfg-$i"
    args=(--name "$name" --runtime binary --wait 3m --kubeconfig "$TMP/$name.kubeconfig")
    [[ $cfg == tuned ]] && args+=(--config "$TMP/tuned.yaml")
    t0=$(date +%s.%N)
    kwokctl create cluster "${args[@]}" >/dev/null 2>&1
    echo "$name $(echo "$(date +%s.%N) - $t0" | bc)" >> "$TMP/created"
  done
done
sleep 45  # let caches warm and GC settle
while read -r name secs; do
  cfg=${name#cp-}; cfg=${cfg%-*}
  e=$(rss "$name" etcd); a=$(rss "$name" kube-apiserver); k=$(rss "$name" kube-controller-manager)
  s=$(rss "$name" kube-scheduler); w=$(rss "$name" kwok-controller)
  echo "$cfg,$name,$e,$a,$k,$s,$w,$((e + a + k + s + w)),$secs" | tee -a "$OUT"
done < "$TMP/created"
while read -r name _; do kwokctl delete cluster --name "$name" >/dev/null 2>&1; done < "$TMP/created"

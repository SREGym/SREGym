#!/usr/bin/env bash
# Tear down a mini cluster started by up.sh, including every pod process,
# mount, network namespace and iptables rule procnode or kube-proxy created.
#
# usage: down.sh [cluster-name]
set -uo pipefail

NAME=${1:-mini}
STATE=${STATE:-/var/lib/procnode-cluster/$NAME}
ROOT=${ROOT:-/var/lib/procnode}

for p in procnode kube-proxy; do
  [[ -f $STATE/$p.pid ]] && kill "$(cat "$STATE/$p.pid")" 2>/dev/null
done
# setsid may fork, so pid files can miss the real process; match by command.
pkill -f "procnode/procnode.py --kubeconfig" 2>/dev/null
pkill -f "$STATE/kube-proxy --kubeconfig" 2>/dev/null
[[ -x $STATE/kube-proxy ]] && "$STATE/kube-proxy" --cleanup >/dev/null 2>&1

# Pod processes live in their own sessions; their cgroups find them all.
for cg in /sys/fs/cgroup/memory/procnode/*/; do
  [[ -d $cg ]] || continue
  xargs -r kill -9 < "$cg/cgroup.procs" 2>/dev/null
done
sleep 1
grep -oE " $ROOT/[^ ]+" /proc/mounts | sort -r | xargs -r -n1 umount -l 2>/dev/null
grep -oE " /run/procnode/ns/[^ ]+" /proc/mounts | xargs -r -n1 umount -l 2>/dev/null
for ns in $(ip netns list | awk '/^pn-/{print $1}'); do ip netns del "$ns"; done
for c in memory cpu cpuacct; do
  find /sys/fs/cgroup/$c/procnode -mindepth 1 -type d -delete 2>/dev/null
done
rm -rf "$ROOT/pods" /run/procnode/ns/*

kwokctl delete cluster --name "$NAME" >/dev/null 2>&1
echo "mini cluster $NAME removed (images kept in $ROOT/images)"

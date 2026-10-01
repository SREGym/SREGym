#!/bin/sh
# One Kubernetes node with private network, filesystem state, cgroups and hostname.
set -e
name=$1; ip=$2; cgroup_version=$3
until ip link show eth0 >/dev/null 2>&1; do sleep 0.1; done
ip link set lo up
ip addr add $ip/24 dev eth0
ip link set eth0 up
ip route add default via 10.250.0.1 dev eth0  # k3s needs a default route; the fabric drops it
hostname $name

# Remount cgroupfs so this node sees its own subtree as the root.
if [ "$cgroup_version" = 2 ]; then
    mount -t cgroup2 cgroup2 /sys/fs/cgroup
    mkdir -p /sys/fs/cgroup/init
    for p in $(cat /sys/fs/cgroup/cgroup.procs); do echo "$p" > /sys/fs/cgroup/init/cgroup.procs 2>/dev/null || true; done
    for c in $(cat /sys/fs/cgroup/cgroup.controllers); do echo "+$c" > /sys/fs/cgroup/cgroup.subtree_control; done
else
    mount -t tmpfs -o mode=755 cgroup /sys/fs/cgroup
    for c in cpu cpuacct cpuset memory devices freezer blkio pids; do
        mkdir -p /sys/fs/cgroup/$c; mount -t cgroup -o $c cgroup /sys/fs/cgroup/$c
    done
    mkdir -p /sys/fs/cgroup/systemd
    mount -t cgroup -o none,name=systemd cgroup /sys/fs/cgroup/systemd
fi

# Node-private state, kept on the container's volume.
d=/var/lib/rancher/k3s/nodes/$name
mkdir -p $d/k3s/agent/images $d/kubelet $d/etc-rancher $d/log $d/cni /var/lib/kubelet /etc/rancher /var/lib/cni
[ -s $d/machine-id ] || tr -d - < /proc/sys/kernel/random/uuid > $d/machine-id
touch /etc/machine-id
mount --bind $d/machine-id /etc/machine-id
# Air-gapped image tarballs; k3s imports agent/images/*.tar at startup.
for t in /var/lib/rancher/k3s/airgap/*.tar; do [ -f "$t" ] && ln -f "$t" $d/k3s/agent/images/ 2>/dev/null || cp -n "$t" $d/k3s/agent/images/ 2>/dev/null || true; done
mount --bind $d/kubelet /var/lib/kubelet
mount --bind $d/etc-rancher /etc/rancher
mount --bind $d/log /var/log
mount --bind $d/cni /var/lib/cni
mount --bind $d/k3s /var/lib/rancher/k3s
mount -t tmpfs tmpfs /run
mount -t tmpfs tmpfs /tmp
mount --make-rshared /

common="--node-ip=$ip --token=${K3S_TOKEN:-userns-k3s}
    --kubelet-arg=cgroup-driver=cgroupfs
    --kubelet-arg=feature-gates=KubeletInUserNamespace=true
    --kube-proxy-arg=conntrack-max-per-core=0"
if [ "$name" = server ]; then
    exec k3s server $common --disable=traefik,servicelb,metrics-server,local-storage \
        --write-kubeconfig-mode=600 --tls-san=${K3S_TLS_SAN:-kubernetes}
else
    exec k3s agent $common --server=https://10.250.0.2:6443
fi

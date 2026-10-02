#!/bin/sh
# One Kubernetes node with private network, filesystem state, cgroups and hostname.
set -e
name=$1; ip=$2; cgroup_version=$3; role=$4; server_ip=$5
until ip link show eth0 >/dev/null 2>&1; do sleep 0.1; done
ip link set lo up
ip addr add $ip/24 dev eth0
ip link set eth0 up
ip route add default via 10.250.0.1 dev eth0  # k3s needs a default route; the fabric drops it
hostname $name

# Remount cgroupfs so this node sees its own subtree as the root.
if [ "$cgroup_version" = 2 ]; then
    # Over a tmpfs: the same filesystem cannot be mounted on its own mount root.
    mount -t tmpfs -o mode=755 tmpfs /sys/fs/cgroup
    mount -t cgroup2 cgroup2 /sys/fs/cgroup
    mkdir -p /sys/fs/cgroup/init
    # The fabric moves this node's unshare parent into init; retry until the
    # root holds no process and can delegate controllers.
    for attempt in $(seq 30); do
        for p in $(cat /sys/fs/cgroup/cgroup.procs); do echo "$p" > /sys/fs/cgroup/init/cgroup.procs 2>/dev/null || true; done
        if for c in $(cat /sys/fs/cgroup/cgroup.controllers); do echo "+$c" > /sys/fs/cgroup/cgroup.subtree_control 2>/dev/null || exit 1; done; then
            break
        fi
        [ "$attempt" = 30 ] && { echo "cannot delegate cgroup controllers" >&2; exit 1; }
        sleep 1
    done
    echo "[$name] cgroup controllers: $(cat /sys/fs/cgroup/cgroup.subtree_control)"
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
mkdir -p $d/k3s/agent/images $d/kubelet $d/etc-rancher $d/log $d/cni $d/openebs \
    /var/lib/kubelet /etc/rancher /var/lib/cni /var/openebs
[ -s $d/machine-id ] || tr -d - < /proc/sys/kernel/random/uuid > $d/machine-id
touch /etc/machine-id
mount --bind $d/machine-id /etc/machine-id
# Air-gapped image tarballs; k3s imports agent/images/*.tar at startup.
for t in /var/lib/rancher/k3s/airgap/*.tar; do [ -f "$t" ] && ln -f "$t" $d/k3s/agent/images/ 2>/dev/null || cp -n "$t" $d/k3s/agent/images/ 2>/dev/null || true; done
mount --bind $d/kubelet /var/lib/kubelet
mount --bind $d/etc-rancher /etc/rancher
mount --bind $d/log /var/log
mount --bind $d/cni /var/lib/cni
mount --bind $d/openebs /var/openebs
mount --bind $d/k3s /var/lib/rancher/k3s
mount -t tmpfs tmpfs /run
mount -t tmpfs tmpfs /tmp
# OpenEBS's node-disk-manager mounts /run/udev from the node.
mkdir -p /run/udev
mount --make-rshared /

if [ -n "${NODE_PROXY:-}" ]; then
    # Image pulls only: k3s passes these to containerd, not to pods.
    export CONTAINERD_HTTP_PROXY=$NODE_PROXY CONTAINERD_HTTPS_PROXY=$NODE_PROXY
    export CONTAINERD_NO_PROXY=127.0.0.0/8,10.0.0.0/8,localhost,.svc,.cluster.local
fi

common="--node-ip=$ip --token=${K3S_TOKEN:-userns-k3s}
    --kubelet-arg=cgroup-driver=cgroupfs
    --kubelet-arg=feature-gates=KubeletInUserNamespace=true
    --kube-proxy-arg=conntrack-max-per-core=0"
if [ "$role" = server ]; then
    exec k3s server $common --disable=traefik,servicelb,metrics-server,local-storage \
        --write-kubeconfig-mode=600 --tls-san=${K3S_TLS_SAN:-kubernetes}
else
    exec k3s agent $common --server=https://$server_ip:6443
fi

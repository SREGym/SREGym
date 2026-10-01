#!/bin/sh
# "Fabric" namespace: owns the cgroup tree and the virtual switch connecting the
# nodes. It has no default route and no uplink, so the cluster has no egress.
set -e
lib=/usr/local/lib/userns-k3s
NODES="${NODES:-server worker1 worker2 worker3}"
V1_CTRLS="cpu cpuacct cpuset memory devices freezer blkio pids"

if [ "$(stat -fc %T /sys/fs/cgroup)" = cgroup2fs ]; then
    cgroup_version=2
    # Docker mounts cgroupfs read-only; a fresh mount in our cgroup namespace is writable.
    mount -t cgroup2 cgroup2 /sys/fs/cgroup
    # cgroup v2 forbids processes in a group that delegates controllers.
    mkdir -p /sys/fs/cgroup/fabric
    for p in $(cat /sys/fs/cgroup/cgroup.procs); do echo "$p" > /sys/fs/cgroup/fabric/cgroup.procs 2>/dev/null || true; done
    for c in $(cat /sys/fs/cgroup/cgroup.controllers); do echo "+$c" > /sys/fs/cgroup/cgroup.subtree_control; done
else
    cgroup_version=1
    mount -t tmpfs -o mode=755 cgroup /sys/fs/cgroup
    for c in $V1_CTRLS; do mkdir -p /sys/fs/cgroup/$c; mount -t cgroup -o $c cgroup /sys/fs/cgroup/$c; done
    mkdir -p /sys/fs/cgroup/systemd
    mount -t cgroup -o none,name=systemd cgroup /sys/fs/cgroup/systemd
fi
mount --make-rshared /

ip link set lo up
ip link add br0 type bridge
ip addr add 10.250.0.1/24 dev br0
ip link set br0 up
socat UNIX-LISTEN:/run/outer/apiserver.sock,fork,mode=600 TCP:10.250.0.2:6443 &

i=2
for n in $NODES; do
    ip=10.250.0.$i; i=$((i+1))
    # Each node gets its own cgroup subtree, then its own cgroup, mount, network,
    # PID, UTS and IPC namespaces.
    if [ $cgroup_version = 2 ]; then
        mkdir -p /sys/fs/cgroup/node-$n
        join="echo \$\$ > /sys/fs/cgroup/node-$n/cgroup.procs"
    else
        for c in $V1_CTRLS systemd; do mkdir -p /sys/fs/cgroup/$c/node-$n; done
        cat /sys/fs/cgroup/cpuset/cpuset.cpus > /sys/fs/cgroup/cpuset/node-$n/cpuset.cpus
        cat /sys/fs/cgroup/cpuset/cpuset.mems > /sys/fs/cgroup/cpuset/node-$n/cpuset.mems
        join="for c in $V1_CTRLS systemd; do echo \$\$ > /sys/fs/cgroup/\$c/node-$n/cgroup.procs; done"
    fi
    sh -c "$join; exec unshare --mount --net --uts --ipc --cgroup --pid --fork --mount-proc /bin/sh $lib/node.sh $n $ip $cgroup_version" \
        > /var/log/node-$n.log 2>&1 &
    pid=$!
    until [ "$(readlink /proc/$pid/ns/net)" != "$(readlink /proc/self/ns/net)" ]; do sleep 0.1; done
    ip link add veth-$n type veth peer name eth0 netns $pid
    ip link set veth-$n master br0 up
    echo "[fabric] started node $n ($ip), log: /var/log/node-$n.log"
done
wait

#!/bin/sh
# "Fabric" namespace: owns the cgroup tree and the virtual switch connecting the
# nodes. It has no default route and no uplink, so the cluster has no egress
# beyond the optional image-pull proxy.
set -e
lib=/usr/local/lib/userns-k3s
# KIND-style names: SREGym recognizes an emulated cluster by them.
NODES="${NODES:-kind-control-plane kind-worker kind-worker2 kind-worker3}"
V1_CTRLS="cpu cpuacct cpuset memory devices freezer blkio pids"

if [ "$(stat -fc %T /sys/fs/cgroup)" = cgroup2fs ]; then
    cgroup_version=2
    # Docker mounts cgroupfs read-only; a fresh mount in our cgroup namespace is
    # writable. The kernel refuses the same filesystem on the root of its own
    # mount (EBUSY), so mount it over a tmpfs.
    mount -t tmpfs -o mode=755 tmpfs /sys/fs/cgroup
    mount -t cgroup2 cgroup2 /sys/fs/cgroup
    # cgroup v2 forbids processes in a group that delegates controllers, so move
    # every process in the container (this namespace shares its PIDs) to a leaf.
    mkdir -p /sys/fs/cgroup/fabric
    for attempt in 1 2 3 4 5 6 7 8 9 10; do
        for p in $(cat /sys/fs/cgroup/cgroup.procs); do echo "$p" > /sys/fs/cgroup/fabric/cgroup.procs 2>/dev/null || true; done
        if (for c in $(cat /sys/fs/cgroup/cgroup.controllers); do echo "+$c" > /sys/fs/cgroup/cgroup.subtree_control || exit 1; done); then
            break
        fi
        sleep 1
    done
else
    cgroup_version=1
    mount -t tmpfs -o mode=755 cgroup /sys/fs/cgroup
    for c in $V1_CTRLS; do mkdir -p /sys/fs/cgroup/$c; mount -t cgroup -o $c cgroup /sys/fs/cgroup/$c; done
    mkdir -p /sys/fs/cgroup/systemd
    mount -t cgroup -o none,name=systemd cgroup /sys/fs/cgroup/systemd
fi
mount --make-rshared /
echo "[fabric] cgroup v$cgroup_version: $(cat /sys/fs/cgroup/cgroup.subtree_control 2>/dev/null || echo v1 hierarchies)"

ip link set lo up
ip link add br0 type bridge
ip addr add 10.250.0.1/24 dev br0
ip link set br0 up
server_ip=10.250.0.2
socat UNIX-LISTEN:/run/outer/apiserver.sock,fork,mode=600 TCP:$server_ip:6443 &
if [ -S /run/outer/egress.sock ]; then
    # Nodes reach the image-pull proxy at the bridge address.
    socat TCP-LISTEN:3128,bind=10.250.0.1,fork,reuseaddr UNIX-CONNECT:/run/outer/egress.sock &
    export NODE_PROXY=http://10.250.0.1:3128
fi

i=2
role=server
for n in $NODES; do
    ip=10.250.0.$i; i=$((i+1))
    # Each node gets its own cgroup subtree, then its own cgroup, mount, network,
    # PID, UTS and IPC namespaces.
    if [ $cgroup_version = 2 ]; then
        mkdir -p /sys/fs/cgroup/node-$n/init
        join="echo \$\$ > /sys/fs/cgroup/node-$n/cgroup.procs"
    else
        for c in $V1_CTRLS systemd; do mkdir -p /sys/fs/cgroup/$c/node-$n; done
        cat /sys/fs/cgroup/cpuset/cpuset.cpus > /sys/fs/cgroup/cpuset/node-$n/cpuset.cpus
        cat /sys/fs/cgroup/cpuset/cpuset.mems > /sys/fs/cgroup/cpuset/node-$n/cpuset.mems
        join="for c in $V1_CTRLS systemd; do echo \$\$ > /sys/fs/cgroup/\$c/node-$n/cgroup.procs; done"
    fi
    sh -c "$join; exec unshare --mount --net --uts --ipc --cgroup --pid --fork --mount-proc /bin/sh $lib/node.sh $n $ip $cgroup_version $role $server_ip" \
        > /var/log/node-$n.log 2>&1 &
    pid=$!
    until [ "$(readlink /proc/$pid/ns/net)" != "$(readlink /proc/self/ns/net)" ]; do sleep 0.1; done
    if [ $cgroup_version = 2 ]; then
        # The unshare parent stays outside the node's PID namespace, where the
        # node cannot move it; park it in the node's leaf so the node's root
        # cgroup can delegate controllers. Retried: on Daytona this write once
        # failed with EIO, which ended the fabric and the whole cluster.
        attempt=1
        until echo $pid 2>/dev/null > /sys/fs/cgroup/node-$n/init/cgroup.procs; do
            if [ $attempt -ge 20 ]; then
                echo "[fabric] cannot move node $n's unshare parent ($pid) into its cgroup" >&2
                exit 1
            fi
            attempt=$((attempt + 1))
            sleep 0.5
        done
    fi
    # Interface names are limited to 15 characters.
    ip link add veth$i type veth peer name eth0 netns $pid
    ip link set veth$i master br0 up
    echo "[fabric] started $role $n ($ip), log: /var/log/node-$n.log"
    role=agent
done
wait

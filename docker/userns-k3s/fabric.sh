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
if command -v python3 >/dev/null && [ -f $lib/dns-stub.py ]; then
    # CoreDNS's upstream. Without one it forwards to the container's resolvers,
    # which the nodes cannot reach, and names outside the cluster take seconds
    # to fail (see dns-stub.py).
    python3 $lib/dns-stub.py 10.250.0.1 &
    # Not under /run or /tmp: each node mounts its own.
    mkdir -p /etc/userns-k3s
    echo "nameserver 10.250.0.1" > /etc/userns-k3s/resolv.conf
    export NODE_RESOLV_CONF=/etc/userns-k3s/resolv.conf
fi

# With CAP_SYS_ADMIN and no user namespace around the fabric (the Harbor tasks'
# docker-compose.yaml), give each node its own identity-mapped user namespace,
# owned by a distinct otherwise-unused uid. The kernel charges inotify instances
# to a user namespace's owner, so each node gets the host's per-user limit
# (fs.inotify.max_user_instances, 128 by default) instead of all four sharing
# root's: containerd's per-pod shims alone exceed 128 for the larger
# applications. Holding CAP_SYS_ADMIN exempts the namespace from Ubuntu 24.04's
# restriction on unprivileged user namespaces.
node_userns=""
cap_eff=$(awk '/^CapEff:/ {print $2}' /proc/self/status)
if grep -qE '^ +0 +0 +4294967295$' /proc/self/uid_map && [ $(( (0x$cap_eff >> 21) & 1 )) -eq 1 ]; then
    node_caps=$(setpriv --dump | sed -n 's/^Capability bounding set: //p' | tr ',' '\n' | sed 's/^/+/' | paste -sd, -)
    node_userns=1
fi

i=2
owner=2147000001
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
    userns=""
    if [ -n "$node_userns" ]; then
        # newuidmap, which writes the mapping, wants a name and a subordinate range.
        if ! grep -q "^sregym-$n:" /etc/passwd; then
            echo "sregym-$n:x:$owner:$owner::/:/usr/sbin/nologin" >> /etc/passwd
            echo "sregym-$n:x:$owner:" >> /etc/group
            echo "sregym-$n:0:65536" >> /etc/subuid
            echo "sregym-$n:0:65536" >> /etc/subgid
        fi
        userns="setpriv --reuid=$owner --regid=$owner --clear-groups --inh-caps=$node_caps --ambient-caps=$node_caps"
        userns="$userns unshare --user --map-users=0:0:65536 --map-groups=0:0:65536 --setgroups=allow --setuid=0 --setgid=0"
    fi
    owner=$((owner + 1))
    sh -c "$join; exec $userns unshare --mount --net --uts --ipc --cgroup --pid --fork --mount-proc /bin/sh $lib/node.sh $n $ip $cgroup_version $role $server_ip" \
        > /var/log/node-$n.log 2>&1 &
    pid=$!
    until [ "$(readlink /proc/$pid/ns/net)" != "$(readlink /proc/self/ns/net)" ]; do sleep 0.1; done
    if [ $cgroup_version = 2 ]; then
        # The unshare parent stays outside the node's PID namespace, where the
        # node cannot move it; park it in the node's leaf so the node's root
        # cgroup can delegate controllers. On Daytona this write has failed with
        # EIO in about 1 of 70 starts, and retrying has not helped yet; the
        # failure logs the cgroup state so the cause can be found.
        attempt=1
        until err=$({ echo $pid > /sys/fs/cgroup/node-$n/init/cgroup.procs; } 2>&1); do
            if [ $attempt -ge 20 ]; then
                echo "[fabric] cannot move node $n's unshare parent ($pid) into its cgroup: $err" >&2
                for f in /proc/$pid/cgroup /sys/fs/cgroup/node-$n/cgroup.type /sys/fs/cgroup/node-$n/cgroup.procs \
                    /sys/fs/cgroup/node-$n/cgroup.subtree_control /sys/fs/cgroup/node-$n/init/cgroup.type; do
                    echo "[fabric] $f: $(tr '\n' ' ' < $f 2>&1)" >&2
                done
                grep -E '^(State|NSpid)' /proc/$pid/status >&2
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

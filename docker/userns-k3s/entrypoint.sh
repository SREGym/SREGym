#!/bin/sh
# Runs as container root WITHOUT --privileged, with Docker's default capabilities
# and no devices. Everything Kubernetes needs happens inside a user namespace.
set -e
lib=/usr/local/lib/userns-k3s
mkdir -p /run/outer
# The only way into the cluster: container :6443 -> unix socket -> API server.
# Unix sockets cross network namespaces. Start this before unshare, which moves
# the calling process into the new namespaces.
socat TCP-LISTEN:6443,fork,reuseaddr UNIX-CONNECT:/run/outer/apiserver.sock &
# Identity-mapped user namespace (container root may write a multi-id map because
# it holds CAP_SETUID). Root inside it has full capabilities, but only over the
# namespaces it owns.
exec unshare --user --map-users=0:0:65536 --map-groups=0:0:65536 --setgroups=allow \
    --mount --net --pid --cgroup --uts --ipc --fork --mount-proc --propagation=private \
    /bin/sh $lib/fabric.sh

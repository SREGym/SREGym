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
if [ -n "${EGRESS_PROXY:-}" ]; then
    # Optional way out, for image pulls only: containerd on every node talks to
    # this HTTP proxy (host:port, reachable from the container) through a unix
    # socket. Pods themselves still have no route out.
    socat UNIX-LISTEN:/run/outer/egress.sock,fork,mode=600 "TCP:$EGRESS_PROXY" &
fi
# Identity-mapped user namespace (container root may write a multi-id map because
# it holds CAP_SETUID). Root inside it has full capabilities, but only over the
# namespaces it owns. The fabric keeps the container's PID namespace so it can
# move every process when it delegates cgroup v2 controllers.
exec unshare --user --map-users=0:0:65536 --map-groups=0:0:65536 --setgroups=allow \
    --mount --net --cgroup --uts --ipc --propagation=private \
    /bin/sh $lib/fabric.sh

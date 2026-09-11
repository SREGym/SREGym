#!/usr/bin/env bash
# Enable nested cgroup v2 controllers in the outer container's private namespace.
# Based on the nesting procedure in https://github.com/moby/moby/blob/master/hack/dind.
set -euo pipefail

if [[ -f /sys/fs/cgroup/cgroup.controllers ]]; then
    if [[ $(cat /proc/1/cgroup) != '0::/' ]]; then
        echo 'DinD requires a private cgroup namespace; use --cgroupns=private.' >&2
        exit 1
    fi
    mkdir -p /sys/fs/cgroup/init
    read -ra controllers < /sys/fs/cgroup/cgroup.controllers
    enabled=false
    for ((attempt=0; attempt<10; attempt++)); do
        # cgroup v2 forbids processes in a group that delegates controllers.
        # Ignore processes that exit between reading the list and moving them.
        xargs -rn1 < /sys/fs/cgroup/cgroup.procs > /sys/fs/cgroup/init/cgroup.procs || true
        if printf '+%s ' "${controllers[@]}" > /sys/fs/cgroup/cgroup.subtree_control; then
            enabled=true
            break
        fi
        sleep 1
    done
    if [[ $enabled != true ]]; then
        echo 'Unable to delegate cgroup controllers for nested containers.' >&2
        exit 1
    fi
fi

# Nested systemd and Kubernetes hostPath mounts need shared mount propagation.
mount --make-rshared /

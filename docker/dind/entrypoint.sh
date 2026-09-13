#!/usr/bin/env bash
set -euo pipefail

if [[ $(id -u) != 0 ]]; then
    echo 'SREGym DinD must run as root in a privileged container.' >&2
    exit 1
fi
if [[ ${DOCKER_HOST:-unix:///var/run/docker.sock} != unix:///var/run/docker.sock ]]; then
    echo 'SREGym DinD requires its private local Docker daemon.' >&2
    exit 1
fi
if [[ -S /var/run/docker.sock ]]; then
    echo 'Refusing an existing Docker socket. Do not mount the host Docker socket.' >&2
    exit 1
fi
unset DOCKER_TLS_VERIFY DOCKER_CERT_PATH DOCKER_CONTEXT KUBECONFIG
export DOCKER_HOST=unix:///var/run/docker.sock
export KIND_RETAIN_ON_FAILURE=true
mkdir -p /run/udev /root/.kube /opt/sregym/results/dind
daemon_log=/opt/sregym/results/dind/dockerd.log
daemon_pid=
child_pid=
cleanup() {
    local status=$?
    trap - EXIT TERM INT
    if [[ -n $child_pid ]]; then
        kill -TERM -- "-$child_pid" 2>/dev/null || true
    fi
    if [[ -n $daemon_pid ]]; then
        kill -TERM "$daemon_pid" 2>/dev/null || true
        # Bound shutdown even if dockerd or a nested container is stuck.
        for ((i=0; i<20; i++)); do
            kill -0 "$daemon_pid" 2>/dev/null || break
            sleep 1
        done
        kill -KILL "$daemon_pid" 2>/dev/null || true
        wait "$daemon_pid" 2>/dev/null || true
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

# Delegate only the outer container's private cgroup subtree.
bash /opt/sregym/docker/dind/prepare-cgroups.sh
if [[ -n ${SREGYM_DOCKER_TMPFS_SIZE:-} ]]; then
    # Older kernels' tmpfs lacks user xattrs found in container image layers.
    # A sparse ext4 image in tmpfs provides them without physical disk I/O.
    if ! mountpoint -q /run/sregym-docker-data || \
        [[ $(findmnt -n -o FSTYPE /run/sregym-docker-data) != tmpfs ]]; then
        echo 'Memory-backed Docker requires the launcher --docker-tmpfs-size option.' >&2
        exit 1
    fi
    docker_data_image=/run/sregym-docker-data/docker.img
    truncate -s "$(numfmt --from=iec "${SREGYM_DOCKER_TMPFS_SIZE^^}")" "$docker_data_image"
    mkfs.ext4 -q -F -m 0 -E lazy_itable_init=0,lazy_journal_init=0 "$docker_data_image"
    # mount's loop devices use autoclear and detach when the mount is released.
    mount -o loop,noatime "$docker_data_image" /var/lib/docker
fi
export container=docker
dockerd --host=unix:///var/run/docker.sock \
    --storage-driver="${SREGYM_DOCKER_STORAGE_DRIVER:-overlay2}" >"$daemon_log" 2>&1 &
daemon_pid=$!
ready=false
for ((i=0; i<120; i++)); do
    if docker info >/dev/null 2>&1; then
        ready=true
        break
    fi
    kill -0 "$daemon_pid" 2>/dev/null || break
    sleep 1
done
if [[ $ready != true ]]; then
    cat "$daemon_log" >&2
    echo 'Private Docker daemon failed to start; check privileged mode and cgroup support.' >&2
    exit 1
fi

case "$(uname -m)" in
    x86_64) arch=x86 ;;
    aarch64|arm64) arch=arm ;;
    *) echo 'Unsupported architecture' >&2; exit 1 ;;
esac
# The older custom node images contain containerd 2.0.2, which can deadlock
# while Calico initializes. Build the same udev/socat additions on a patched
# Kubernetes 1.32 base, once per private daemon.
setsid docker build --build-arg "KIND_NODE_IMAGE=${SREGYM_KIND_BASE_IMAGE:-kindest/node:v1.32.11}" \
    -t sregym-kind:local kind &
child_pid=$!
wait "$child_pid"
child_pid=
export KIND_NODE_IMAGE=sregym-kind:local
# etcd is disposable in these per-run clusters. Keeping its small database in
# memory prevents image extraction on the shared host disk from stalling API
# writes. Application volumes still use the daemon's disk-backed storage.
if [[ ${SREGYM_ETCD_TMPFS_SIZE:-512m} != 0 ]]; then
    mkdir -p /run/sregym-etcd
    mount -t tmpfs -o "size=${SREGYM_ETCD_TMPFS_SIZE:-512m}" tmpfs /run/sregym-etcd
    export KIND_CONFIG=/run/sregym-kind.yaml
    python - "$arch" <<'PY'
import os
import sys
from pathlib import Path

import yaml

config = yaml.safe_load(Path(f"kind/kind-config-{sys.argv[1]}.yaml").read_text())
config["nodes"][0].setdefault("extraMounts", []).append(
    {"hostPath": "/run/sregym-etcd", "containerPath": "/var/lib/etcd"}
)
Path(os.environ["KIND_CONFIG"]).write_text(yaml.safe_dump(config))
PY
fi
# Keep the existing four-node topology and Calico behavior used by SREGym.
setsid bash kind/setup_kind_cluster.sh "$arch" &
child_pid=$!
if wait "$child_pid"; then
    child_pid=
else
    status=$?
    child_pid=
    timeout 120 kind export logs /opt/sregym/results/dind/kind-logs || true
    exit "$status"
fi
touch /run/sregym-ready
setsid "$@" &
child_pid=$!
status=0
wait "$child_pid" || status=$?
child_pid=
exit "$status"

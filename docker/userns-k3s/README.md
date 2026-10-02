# Unprivileged multi-node k3s (experimental prototype)

This directory is a feasibility prototype. It is not wired into SREGym.

It runs a four-node Kubernetes cluster (k3s: one server and three agents) inside
one Docker container. The nodes use KIND's names (`kind-control-plane`,
`kind-worker`, `kind-worker2`, `kind-worker3`), so SREGym treats it as its
emulated cluster. The container does **not** use `--privileged`, adds no
capabilities and gets no devices or host mounts. Pods have no route to the
outside world. The only way in is the API server, forwarded to the container's
port 6443. The only way out is optional and serves image pulls only: set
`EGRESS_PROXY` and containerd on every node pulls through that HTTP proxy.

## Why `--privileged` is normally needed

Networking is not the main reason. Docker's default seccomp profile blocks the
syscalls behind *every* container runtime unless the container has
`CAP_SYS_ADMIN`:

- `unshare`, `setns` and `clone` with namespace flags
- `mount`, `umount2` and `pivot_root`
- the new mount API
- `keyctl` and `add_key`

In a default container, `unshare --user true` fails with `EPERM`. So no
containerd, runc or kubelet can start. That is true even for a pod with no
network at all.

Once those syscalls are allowed, root can create a user namespace. Inside it, it
holds every capability, but only over namespaces it owns. That is enough for a
whole cluster. The probes behind this prototype found:

| Operation inside the user namespace | Result |
|---|---|
| tmpfs, overlayfs, bind mounts | Work |
| Fresh `procfs` for a new PID namespace | Works only with `systempaths=unconfined` |
| veth, bridge, vxlan, iptables (nft), tc, `net.*` sysctls | Work |
| Writable cgroups, via a fresh cgroupfs mount in our cgroup namespace | Work |
| Read-write sysfs | **Refused.** Docker mounts `/sys` read-only, so the kernel only allows read-only sysfs (`runc-wrapper` handles this) |
| Host-global sysctls such as `nf_conntrack_max`, kernel modules, `/dev/kmsg` | Unavailable |

## What the outer container needs

| Option | Why |
|---|---|
| `--security-opt seccomp=seccomp.json` | Docker's default profile plus 20 namespace, mount and keyring syscalls. `seccomp=unconfined` also works. |
| `--security-opt systempaths=unconfined` | Docker's masked `/proc` paths stop a user namespace from mounting a fresh `procfs`, and every pod needs one. |
| `--cgroupns=private` | Gives the container its own cgroup subtree to delegate to nodes and pods. |

`seccomp.json` is derived from moby's default profile
(<https://github.com/moby/profiles>, Apache-2.0). It adds one allow rule and
drops the `CAP_SYS_ADMIN`-conditional `clone`/`clone3` rules that the new rule
replaces.

On AppArmor hosts (Ubuntu), the `docker-default` profile denies `mount`.
Ubuntu 24.04 also restricts unprivileged user namespaces. Expect to need an
AppArmor profile that allows `mount` and `userns`. This is untested.

## How it works

- **`entrypoint.sh`** starts a forwarder from the container's `:6443` to a unix
  socket. It then creates an identity-mapped user namespace with new mount,
  network, PID, cgroup, UTS and IPC namespaces.
- **`fabric.sh`** mounts a writable cgroup tree and creates a bridge with no
  uplink. It then starts each node in its own cgroup subtree and its own mount,
  network, PID, UTS, IPC and cgroup namespaces, connected to the bridge by a veth.
- **`node.sh`** gives each node private state:
  - `/var/lib/rancher/k3s`, `/var/lib/kubelet`, `/run`, a machine ID and a hostname
  - a default route that leads nowhere

  It then runs `k3s server` or `k3s agent` with the `KubeletInUserNamespace`
  feature gate.
- **`runc-wrapper`** rewrites the read-write `/sys` that privileged pods ask for
  into a read-only one. Privileged pods include CNI agents, chaos daemons and
  node exporters.

Images come from one of two places:

- **Preloaded tarballs.** k3s imports every tarball found in
  `/var/lib/rancher/k3s/airgap/` at startup.
- **An image-pull proxy.** With `EGRESS_PROXY=host:port`, `entrypoint.sh`
  forwards a unix socket to that proxy, and `fabric.sh` exposes it on the
  bridge. Each node's containerd then gets it as `CONTAINERD_HTTP(S)_PROXY`,
  which k3s does not pass to pods. `egress-proxy.py` is a minimal proxy to
  run on the host. Mount a containerd `registries.yaml` at
  `/etc/userns-k3s/registries.yaml` to add mirrors or rewrites (see
  `registries.yaml`).

`node.sh` also matches two KIND behaviors that SREGym depends on:

- **Control-plane label.** It gives the control plane KIND's empty
  `node-role.kubernetes.io/control-plane` label, which SREGym's charts select.
- **No disk-based eviction or image GC.** The nodes share the container's
  filesystem, so its free share says nothing about any one node.

## Run

```bash
cd docker/userns-k3s
docker build -t sregym-userns-k3s .

docker volume create userns-k3s
docker pull rancher/mirrored-pause:3.6
docker pull rancher/mirrored-coredns-coredns:1.12.1
docker pull nginx:1.27-alpine
docker pull busybox:1.36
docker pull nicolaka/netshoot:latest
docker save rancher/mirrored-pause:3.6 rancher/mirrored-coredns-coredns:1.12.1 \
    nginx:1.27-alpine busybox:1.36 nicolaka/netshoot:latest -o /tmp/airgap.tar
docker run --rm -v userns-k3s:/data -v /tmp/airgap.tar:/airgap.tar:ro --entrypoint sh \
    rancher/k3s:v1.32.5-k3s1 -c 'mkdir -p /data/airgap && cp /airgap.tar /data/airgap/images.tar'

docker network create userns-k3s
docker run -d --name k3s --network userns-k3s -e K3S_TLS_SAN=k3s \
    --security-opt seccomp=$PWD/seccomp.json --security-opt systempaths=unconfined \
    --cgroupns=private -v userns-k3s:/var/lib/rancher/k3s sregym-userns-k3s

# The kubeconfig is written once the server is up.
docker exec k3s cat /var/lib/rancher/k3s/nodes/kind-control-plane/etc-rancher/k3s/k3s.yaml \
    | sed 's#https://127.0.0.1:6443#https://k3s:6443#' > /tmp/userns-k3s.yaml

# Run as an "agent": a separate container with default security settings.
docker run --rm --network userns-k3s -v /tmp/userns-k3s.yaml:/kc:ro -v $PWD/smoke.sh:/smoke.sh:ro \
    -e KUBECONFIG=/kc --entrypoint sh rancher/k3s:v1.32.5-k3s1 /smoke.sh
```

## Validation status

Validated on a Firecracker VM (kernel 6.18, cgroup v1 hybrid, no AppArmor or
SELinux, Docker 29.6.2). The outer container ran with
`Privileged=false CapAdd=[] Devices=[]` and `seccomp.json`. All 10 checks in
`smoke.sh` passed:

- 4 Ready nodes
- pods spread across 3 nodes, with pod-to-pod traffic across nodes
- Service plus CoreDNS
- no internet egress
- NetworkPolicy enforcement and removal
- a memory limit that OOM-kills
- a privileged `hostPID` pod that `nsenter`s another pod's netns and adds a tc
  qdisc that really throttles it

Two more manual checks passed:

- **Freezer fault.** Freezing one node's freezer cgroup made it `NotReady`
  within 43 seconds; thawing it restored the node. This is the analogue of
  `kubelet_crash`.
- **Footprint.** The idle 4-node cluster uses about 840 MiB of memory.

Not validated:

- **cgroup v2 hosts.** The cgroup v2 path is written but untested; it mirrors
  `docker/dind/prepare-cgroups.sh`.
- **AppArmor or SELinux hosts.**
- **Calico.**
- **Any SREGym application or problem.**

## Gaps before SREGym could use this

- **CNI.** k3s ships flannel and kube-router network policies. SREGym installs
  Calico. Its filtered agent mode needs Calico Tiers and a GlobalNetworkPolicy,
  and two problems use Calico CRDs. Calico would need to be installed with
  `--flannel-backend=none`, or this could become *kind on a dockerd running
  inside the user namespace*. The kind route keeps SREGym's existing
  `kind-config.yaml`, Calico setup and `docker exec` node faults. It needs the
  same read-only-sysfs runc wrapper for the kind node containers.
- **Node faults.** Faults that `docker exec` into KIND nodes would need
  `nsenter` or cgroup-freezer equivalents. This affects `kubelet_crash` and
  `kubelet_eviction_threshold_misconfig`.
- **Problems that are not portable:**
  - `node_conntrack_exhaustion` writes a host-global sysctl.
  - `workload_imbalance` replaces the kube-proxy DaemonSet; k3s has kube-proxy
    built in.
- **Kernel modules.** Every module must already be loaded on the host,
  including `sch_netem` for Chaos Mesh delay and `ipip` for Calico IPIP.
- **Air-gapped images.** Every image, chart and manifest must be preloaded, or
  served by a pull-through mirror reachable from the fabric.

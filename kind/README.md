# Running SREGym with KIND

SREGym supports KIND on Linux, WSL2, and macOS. The provided setup creates one control-plane node, three worker nodes, and installs Calico for networking and NetworkPolicy support.

## Requirements

Install the dependencies listed in the [main README](../README.md): Python 3.12 or newer, Docker, KIND, kubectl, Helm 4.0 or newer, and uv.

## Linux and WSL2 host settings

KIND nodes share the host's inotify limits. On Linux and WSL2, low defaults can cause system pods to crash with `too many open files`.

Set the recommended values before creating the cluster:

```bash
sudo sysctl -w fs.inotify.max_user_instances=1024
sudo sysctl -w fs.inotify.max_user_watches=1048576
```

To keep the values after rebooting:

```bash
echo "fs.inotify.max_user_instances=1024" | sudo tee /etc/sysctl.d/99-sregym-kind.conf
echo "fs.inotify.max_user_watches=1048576" | sudo tee -a /etc/sysctl.d/99-sregym-kind.conf
sudo sysctl --system
```

These host commands are not needed on macOS.

## Create the cluster

From the repository root, run the command matching your machine:

```bash
# x86-64 Linux, WSL2, or Intel Mac
bash kind/setup_kind_cluster.sh x86

# ARM64 Linux or Apple silicon Mac
bash kind/setup_kind_cluster.sh arm
```

The script:

1. creates the four-node KIND cluster using the matching architecture image;
2. installs Calico;
3. waits for Calico and all nodes to become ready; and
4. clears the previous SREGym cluster-baseline cache.

Confirm that all four nodes are `Ready`:

```bash
kubectl get nodes
```

The cluster is now ready to run SREGym.

## Troubleshooting

### Khaos problems

Khaos checks the capabilities of every required node. Worker pods use nodes
without the control-plane or legacy master role; a worker role label is not
required. The manifest creates `/var/openebs` when it is absent.

eBPF problems require a Khaos image containing the nested PID namespace support
and `--check` interface from Khaos PR #37. An older image is a deployment error,
not an unsupported host kernel. To test an unpublished image after building it
from the desired Khaos checkout:

```bash
kind load docker-image khaos:pr37-test --name kind
export KHAOS_IMAGE=khaos:pr37-test
export KHAOS_IMAGE_PULL_POLICY=Never
```

For a published image, `KHAOS_IMAGE` can instead contain an immutable registry
digest. Leave `KHAOS_IMAGE_PULL_POLICY` unset to retain the manifest's policy.

Silent data corruption requires the kernel's `random_read_corrupt` and
`random_write_corrupt` dm-flakey features. Merely loading `dm_flakey` is not
sufficient. Preflight creates and removes a small disposable device to verify
formatting, mounting, and the requested table features. Older kernels can
support basic dm-flakey while rejecting random corruption; those hosts are
reported as unsupported before application deployment.

kind nodes share device-mapper and loop devices. SREGym uses node-UID-specific
device names and backing files, skips udev synchronization, and creates device
nodes explicitly. The faulted application's PVCs use the non-default
`sregym-dm-flakey` storage class at `/var/openebs/khaos`; observability storage
stays on its original class. Stop the application before removing its devices.

### Docker issues

Ensure Docker is running and accessible to your user:

```bash
docker ps
```

### Cluster creation failures

Check that Docker is correctly installed and that your system has enough CPU and memory. Export the KIND logs for diagnostics:

```bash
kind export logs ./kind-logs --name kind
```

### Deployment problems

Inspect the pods and recent Kubernetes events, then use `kubectl logs <pod-name>` to view the logs for a failing pod:

```bash
kubectl get pods -A
kubectl get events -A --sort-by='.lastTimestamp'
```

### kube-proxy reports `CrashLoopBackOff` or `too many open files`

All KIND nodes share the host's `fs.inotify.max_user_instances` limit. When this limit is exhausted, new pods that need inotify instances, such as kube-proxy, crash immediately. Apply the Linux or WSL2 inotify settings above.

### Resource allocation

WSL2 may require additional resources. Adjust the WSL2 settings in your `.wslconfig` file on Windows if you encounter performance issues.

### Deployment timeout on a slow network

If you have a slow local network connection, first-time deployments may timeout while pulling container images. Increase the timeout in your `.env` file:

```bash
WAIT_FOR_POD_READY_TIMEOUT=1800  # 30 minutes (recommended for slow networks)
```

Subsequent deployments are faster since images are cached. Remote clusters typically don't need this adjustment.

### The cluster already exists

Delete the existing KIND cluster using the command below before recreating it.

## Delete the cluster

```bash
kind delete cluster --name kind
```

This removes the KIND node containers. Docker images remain cached.

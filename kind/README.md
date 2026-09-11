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

From the repository root, let the setup script select the correct node image:

```bash
bash kind/setup_kind_cluster.sh
```

You can also select the architecture explicitly:

```bash
# x86-64 Linux, WSL2, or Intel Mac
bash kind/setup_kind_cluster.sh x86

# ARM64 Linux or Apple silicon Mac
bash kind/setup_kind_cluster.sh arm
```

The script:

1. creates the four-node KIND cluster using the published multiarch node image;
2. installs Calico;
3. waits for Calico and all nodes to become ready; and
4. clears the previous SREGym cluster-baseline cache.

Confirm that all four nodes are `Ready`:

```bash
kubectl get nodes
```

SREGym-Lite pulls published multiarch application images directly. No local
build-and-load step is needed on Apple silicon. See
[SREGym-Lite](../docs/SREGym-Lite.md) for the supported problem set and
[container images](../docs/container-images.md) for optional local source builds.
The [multiarch validation report](../docs/macOS-multiarch-validation.md) covers
the wider application catalog and lists the remaining source and infrastructure
requirements.

## Troubleshooting

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

On macOS, allocate at least 16 GB to Docker Desktop or OrbStack's Linux VM for
the full Lite profile, leaving additional RAM for macOS and the agent. Keep the
Mac awake during long campaigns; `caffeinate -i <command>` prevents idle sleep
for the lifetime of that command.

WSL2 may require additional resources. Adjust the WSL2 settings in your `.wslconfig` file on Windows if you encounter performance issues.

### Astronomy Shop's flag UI is OOMKilled immediately

Some KIND/containerd configurations give containers an extremely high file
descriptor limit. Erlang can exhaust memory at startup because of that limit.
The Astronomy Shop compatibility values cap the UI process's descriptor limit
at 65,536 while retaining its original 250 MiB memory limit. Simply increasing
the container's memory is not sufficient. Ensure the current compatibility
values are applied by deploying through SREGym.

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

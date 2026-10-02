# sregym/{{task_name}}

An incident in the **{{app_name}}** application, from
[SREGym](https://github.com/SREGym/SREGym), a live benchmark for AI SRE agents.
The agent gets `kubectl` access to a running Kubernetes cluster where a fault
has been injected. It must find the root cause and mitigate it.

The reward is 1.0 when the problem's mitigation oracle finds the application
healthy again after the agent stops, and 0.0 otherwise.

## Runtime

A privileged `sregym` sidecar runs its own Docker daemon and a four-node KIND
cluster, deploys the application and injects the fault. The agent reaches the
cluster only through a filtered Kubernetes API proxy.

The task needs a Harbor environment that runs Docker Compose with privileged
services, such as `docker`, `ec2`, `gke` (Standard) or `daytona`. Request about
{{cpus}} CPUs, {{memory_mb}} MiB of memory and {{storage_mb}} MiB of disk.
Setup pulls images and Helm charts and takes 5 to 30 minutes before the agent
starts. See the
[Harbor guide](https://github.com/SREGym/SREGym/blob/main/docs/harbor.md).

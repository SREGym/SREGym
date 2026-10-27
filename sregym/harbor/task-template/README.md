# sregym/{{task_name}}

An incident in the **{{app_name}}** application, from
[SREGym](https://github.com/SREGym/SREGym), a live benchmark for AI SRE agents.
The agent gets `kubectl` access to a running Kubernetes cluster where a fault
has been injected. It must find the root cause and mitigate it.

The reward is 1.0 when the problem's mitigation oracle finds the application
healthy again after the agent stops, and 0.0 otherwise.

## Runtime

One container runs everything: a four-node k3s cluster, the application and
SREGym's grader. The agent runs as an unprivileged user and reaches the cluster
only through a filtered Kubernetes API proxy.

Run it with `harbor run ... -e docker`; the task ships the Docker settings its
cluster needs in `environment/docker-compose.yaml` (two added capabilities, not
`--privileged`). It uses up to {{cpus}} CPUs, {{memory_mb}} MiB of memory and
{{storage_mb}} MiB of disk, and pulls images and Helm charts before the agent
starts. See the [Harbor guide](https://github.com/SREGym/SREGym/blob/main/docs/harbor.md).

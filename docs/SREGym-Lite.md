# SREGym-Lite

SREGym-Lite is a curated set of 21 well-tested problems with varied difficulty and failure mechanisms. The problems were selected to be easy and reliable to run, making Lite a practical starting point before running the full benchmark.

SREGym-Lite can run using SREGym's existing [KIND](https://kind.sigs.k8s.io/) setup on a machine with 8 vCPU and 16 GB of memory.

To keep setup reliable and resource requirements manageable, the problem set excludes TrainTicket, hardware, and other faults that are difficult to run consistently on all machines.

## Hardware requirements

These resources apply to the machine hosting the KIND cluster, not to each virtual KIND node.

| | vCPU | Memory | Disk | Example EC2 instance |
|---|---:|---:|---:|---|
| Minimum | 8 | 16 GB | 100 GB | `c7a.2xlarge` |
| Recommended | 16 | 32 GB | 200 GB | `c7a.4xlarge` |

The minimum is sufficient for running the benchmark. The recommended configuration provides headroom for additional tools, logs, and cached container images.

## Software requirements

Follow the [main installation instructions](../README.md#📦installation) to install Python 3.12 or newer, Docker, KIND, kubectl, Helm 4.0 or newer, and uv.

## Set up KIND

On Linux and WSL2, raise the host inotify limits before creating the cluster:

```bash
sudo sysctl -w fs.inotify.max_user_instances=1024
sudo sysctl -w fs.inotify.max_user_watches=1048576
```

Create the cluster from the repository root:

```bash
bash kind/setup_kind_cluster.sh
```

The setup creates one control-plane and three worker nodes. Confirm that all four nodes are ready:

```bash
kubectl get nodes
```

The Lite applications, traffic generators, KIND node, and agent runtime use
published multiarch images. Docker and Kubernetes select `linux/arm64` on Apple
silicon or `linux/amd64` on x86-64. No local application-image build or registry
login is required. See [container images](container-images.md) for the pinned
releases and optional source-build commands.

On macOS, the containers run inside Docker Desktop or OrbStack's Linux VM.
Allocate the CPU and memory listed above to that VM. On smaller machines,
`--profile svelte` reduces the bundled observability services for local
experiments; it is not intended for leaderboard submissions.

See the [KIND guide](../kind/README.md) for installation details and troubleshooting.

For model-free lifecycle checks and agent-container checks, see
[local validation](../tests/integration/README.md).

## Run the benchmark

Set the API credentials required by your model as described in [Running an Agent](../README.md#running-an-agent), then run:

```bash
uv run main.py --suite sregym-lite --agent claudecode --model claude-sonnet-5
```

The normal runner options, including `--judge-model`, `--reasoning-effort`, `--n-attempts`, and `--resume`, can be used with `--suite sregym-lite`.

## Included problems

- `cronjob_sidecar_blocks_completion_hotel_reservation`
- `edge_request_filter_cpu_saturation`
- `network_policy_block`
- `env_variable_shadowing_astronomy_shop`
- `mutating_webhook_resource_limits_social_network`
- `finalizer_deadlock_controller_hotel_reservation`
- `kafka_poison_pill_hol_block`
- `internal_traffic_policy_local_astronomy_shop`
- `service_dns_resolution_failure_social_network`
- `service_wrong_pod_selection_hotel_reservation`
- `namespace_memory_limit`
- `valkey_auth_disruption`
- `secret_rotation_stale_env_credentials_astronomy_shop`
- `unschedulable_incorrect_port_assignment`
- `readiness_probe_misconfiguration_social_network`
- `duplicate_pvc_mounts_social_network`
- `admission_webhook_outage_hotel_reservation`
- `wrong_dns_policy_astronomy_shop`
- `wrong_service_selector_social_network`
- `rolling_update_misconfigured_social_network`
- `search_rate_retry_collapse_hotel_reservation`

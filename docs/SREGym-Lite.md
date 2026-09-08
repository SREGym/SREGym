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
# Auto-detect x86-64 or ARM64 (recommended)
bash kind/setup_kind_cluster.sh
```

Or select the architecture explicitly:

```bash
# x86-64
bash kind/setup_kind_cluster.sh x86

# ARM64
bash kind/setup_kind_cluster.sh arm
```

The setup creates one control-plane and three worker nodes. Confirm that all four nodes are ready:

```bash
kubectl get nodes
```

On Apple silicon, build and load the application images that are not yet
published for ARM64:

```bash
bash kind/build_lite_images.sh
```

Run this after creating the KIND cluster. The script builds native images for
Hotel Reservation, Social Network, wrk2, and the Locust exporter, then loads them
into every KIND node. The first Social Network build compiles its legacy C++
dependencies from source and can take a while; later builds reuse Docker's
cache.

On macOS, the containers run inside Docker Desktop or OrbStack's Linux VM.
Allocate the CPU and memory listed above to that VM. On smaller machines,
`--profile svelte` reduces the bundled observability services for local
experiments; it is not intended for leaderboard submissions.

See the [KIND guide](../kind/README.md) for installation details and troubleshooting.

### Validate a local installation without model calls

On a **disposable local KIND cluster**, run the deployment, workload, fault,
mitigation-oracle, recovery, and cleanup checks for all 21 Lite problems:

```bash
uv run python tests/integration/validate_lite.py \
  --profile full --output-dir .runtime/lite-validation/full
```

This runs serially, includes Loki/Promtail, and makes no LLM requests. It deletes
application namespaces and exercises cluster-scoped faults, so do not use a
cluster containing other work. Logs, per-problem Markdown/JSON reports, and an
aggregate `suite.json` are saved in the output directory. It stops on the first
failure; inspect the report and verify cleanup before adding `--resume` to
continue. Resume requires the same cluster node identities and profile.

On macOS, prefix the command with `caffeinate -i` to prevent idle sleep while it
runs. Explicit sleep or closing the lid can still interrupt the Linux VM. Use
`--profile svelte` and a different output directory to validate that profile.

After deployment, check the native agent container's Kubernetes and observability
connections in both open and filtered network modes:

```bash
uv run pytest tests/integration/test_agent_connectivity.py -m integration -v
```

This requires the agent image and the shared monitoring/MCP stack installed by
the lifecycle runner. It queries Kubernetes, Prometheus, Loki, and Jaeger without
installing an agent CLI or contacting a model provider.

To verify the five supported CLI installers and their native startup paths,
without authentication or model calls:

```bash
uv run pytest tests/integration/test_agent_cli_bootstrap.py -m integration -v
```

The agent image build selects native kubectl for the container architecture and
matches the host's kubectl version when available. You can pin a version explicitly
with `KUBECTL_VERSION=v1.33.9 bash docker/agents/build.sh`. Keep the host client
within one minor version of the API server, as required by Kubernetes'
[version-skew policy](https://kubernetes.io/releases/version-skew-policy/#kubectl).

### macOS validation scope

All **21 Lite fault lifecycles passed with the full profile** on Apple silicon,
OrbStack, and a four-node ARM64 KIND cluster with 16 GiB allocated to the Linux
VM. Each check deployed the application, injected the fault, verified oracle
failure, recovered it, verified oracle success, and cleaned up. Native workloads,
agent-container Kubernetes/MCP connectivity, and all five agent CLI installers
were also checked.

These checks use the built-in recovery functions, not an LLM-driven agent
campaign. Docker Desktop and Intel Mac hardware were not tested. See the
[validation report](macOS-Lite-validation.md) for the environment, complete
problem list, reproduction commands, and limitations.

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

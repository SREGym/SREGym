# Running SREGym on Harbor

[Harbor](https://harborframework.com) runs agent benchmarks as self-contained
tasks, locally or on cloud sandboxes, with many trials in parallel. SREGym can
generate a Harbor task for every problem that runs on an emulated cluster. Each
task carries its own private four-node KIND cluster, so trials do not share
state and can run concurrently.

The integration targets Harbor 0.23 (task `schema_version = "1.4"`). It builds
on the [Docker-in-Docker runtime](../docker/dind/README.md).

## How a task works

```
                     Harbor trial (one Compose project)
  ┌──────────────────────────────┐        ┌──────────────────────────────────────┐
  │ main (agent)                 │        │ sregym (privileged sidecar)          │
  │ ubuntu + kubectl             │ HTTPS  │ docker/dind image                    │
  │ KUBECONFIG=/run/sregym/...  ─┼───────►│ :16443 SREGym K8s API proxy          │
  │                              │        │   (hides chaos/load-gen resources)   │
  │ solution/solve.sh (oracle) ──┼───────►│ :8765 /oracle/recover (token)        │
  │                              │        │ 127.0.0.1:8766 /grade (collect hook) │
  │ /run/sregym (read-only) ◄────┼─volume─┤ private dockerd ─► KIND cluster      │
  └──────────────────────────────┘        └──────────────────────────────────────┘
```

1. **Setup.** Harbor starts both services. The sidecar's entrypoint starts a
   private Docker daemon and KIND cluster. It then runs
   `python -m sregym.harbor.backend`, which calls the Conductor's `start_problem()`
   exactly as `main.py` does. That deploys the application, captures the oracle's
   baseline and injects the fault. The backend then starts SREGym's filtered
   Kubernetes API proxy and publishes an agent kubeconfig on a shared volume.
2. **Readiness.** The task's `[environment.healthcheck]` runs `sregym-ready` in
   the agent container. It blocks until the fault is live and the cluster is
   reachable, and fails at once if setup failed. Harbor starts the agent clock
   only after this.
3. **Agent.** The agent sees the application name, namespaces and description,
   the same information SREGym's own agents get. It works through `kubectl`
   against the proxy. It cannot reach the sidecar's Docker daemon, KIND nodes,
   grading code or problem definitions.
4. **Grading.** Harbor stops the agent container, then runs a
   `[[verifier.collect]]` hook inside the sidecar. The hook asks the
   still-running backend to evaluate the problem's mitigation oracle. That
   oracle is the same object that captured the pre-fault baseline, which a
   fresh process could not reconstruct. The verdict is collected as an
   artifact and scored by a separate verifier container. The reward is 1.0 when
   the mitigation oracle succeeds and 0.0 otherwise. If the backend could not
   grade, no reward is written, so Harbor reports an error instead of a zero.
5. **Reference solution.** Harbor's oracle agent runs `solution/solve.sh`. It
   asks the sidecar to run the problem's own `recover_fault()`. The endpoint
   needs a per-task token: an HMAC of the task name keyed by the dataset's
   oracle secret. The task carries only the token's SHA-256. The secret reaches
   `solve.sh` at run time through task.toml's `[solution] env`, which Harbor
   resolves for the oracle agent only. A published task therefore holds no
   usable token.

   The adapter reads the secret from `SREGYM_ORACLE_SECRET`. If that is unset,
   it generates a secret and saves it in `.sregym-oracle-secret` at the dataset
   root, which `harbor publish` never uploads. Export the secret before running
   the oracle agent.

The generated `instruction.md` never names the fault or problem ID. The problem
ID appears only in files that stay on the Harbor host: `task.toml` metadata and
the Compose file.

## Generate tasks

```bash
# Every eligible problem (123 of 125 today)
uv run python -m sregym.harbor.adapter --output-dir datasets/sregym

# SREGym-Lite, or selected problems
uv run python -m sregym.harbor.adapter --suite sregym-lite --output-dir datasets/sregym-lite
uv run python -m sregym.harbor.adapter --task-ids network_policy_block incorrect_image --output-dir datasets/sregym

# A two-minute check that a Harbor environment can run SREGym at all
uv run python -m sregym.harbor.adapter --self-test --output-dir datasets/sregym-selftest
```

No cluster is needed; problems are inspected against a placeholder kubeconfig.
The generator skips problems it cannot turn into working tasks and prints the
reason:

- problems that need a non-emulated cluster
- problems whose constructor queries a live cluster (currently `taint_no_toleration_social_network`)

Other options:

| Option | Default | Meaning |
|---|---|---|
| `--backend-image` | `ghcr.io/sregym/sregym-dind:latest` | Sidecar image. `SREGYM_HARBOR_IMAGE` overrides it when a task runs. |
| `--kind-node-image` | empty | Prebuilt KIND node image (see [Build the images](#build-the-images)). Empty means each trial builds `kind/Dockerfile` during setup, which is slower and needs Ubuntu's package mirrors. |
| `--registry-mirror` | `https://mirror.gcr.io` | Docker Hub pull-through mirror for the sidecar's daemon and every KIND node. Images the mirror lacks are pulled from Docker Hub. Pass `''` to pull from Docker Hub directly. |
| `--agent-timeout` | `1800` | Agent time limit in seconds, matching SREGym's runner. |
| `--cpus` / `--memory-mb` / `--storage-mb` | `8` / `16384` / `51200` | Resources requested for the whole task. Cloud providers size the sandbox from these. |
| `--limit`, `--overwrite` | | Standard Harbor adapter flags. |

Task names are `sregym/<problem-id>`, lowercased with `_` replaced by `-`.

## Build the images

The sidecar runs the DinD image with the SREGym checkout baked in, so rebuild
it whenever problems or oracles change.

**For cloud providers**, run the **Publish Harbor Images** workflow
(`.github/workflows/publish-harbor-images.yml`, from the Actions tab). It
pushes two images:

- `ghcr.io/sregym/sregym-dind:<tag>`
- `ghcr.io/sregym/kind-node:v1.32.11-<tag>`, a prebuilt KIND node image

The workflow summary prints the matching adapter command. Cloud sandboxes pull
anonymously, so make both GHCR packages public the first time they are
published. Generate tasks with both `--backend-image` and `--kind-node-image`
pointing at the published tag, so a dataset always pins the SREGym version
that produced it.

**For local runs**:

```bash
git submodule update --init --recursive
python3 docker/dind/run.py build                  # tags sregym-dind:local
```

Then either generate with `--backend-image sregym-dind:local` or export
`SREGYM_HARBOR_IMAGE=sregym-dind:local`.

## Run with Harbor

```bash
uv tool install harbor            # 0.23 or newer

# Reference solution: every task should score 1.0. The oracle agent needs
# the secret the adapter saved beside the tasks.
export SREGYM_ORACLE_SECRET=$(cat datasets/sregym-selftest/.sregym-oracle-secret)
harbor run -p datasets/sregym-selftest -a oracle
export SREGYM_ORACLE_SECRET=$(cat datasets/sregym/.sregym-oracle-secret)
harbor run -p datasets/sregym/network-policy-block -a oracle

# No-op agent: every task should score 0.0
harbor run -p datasets/sregym/network-policy-block -a nop

# A real agent, several tasks at a time
harbor run -p datasets/sregym-lite -a claude-code -m anthropic/claude-sonnet-5 -n 4
```

On the local Docker backend, `cpus` and `memory_mb` become limits on the agent
container. On a host with fewer than 8 CPUs, pass `--cpus ignore --memory ignore`
or `--override-cpus N`. Each trial needs about 8 CPUs and 16 GiB for the
sidecar. Size `-n` to the host, and raise the host's inotify limits as the
[KIND guide](../kind/README.md) describes. Setup takes several minutes per trial
because every trial starts with an empty image cache.

### Sidecar options

Generated tasks already set the registry mirror and node image from the
adapter flags above. To change these or other sidecar settings for one run, put
a Compose overlay on the `sregym` service and pass it with
`harbor run --extra-docker-compose overlay.yaml`:

| Variable | Purpose |
|---|---|
| `SREGYM_REGISTRY_MIRROR` | Docker Hub pull-through mirror for the private daemon and every KIND node. Defaults to `--registry-mirror`. Set it empty to pull from Docker Hub directly. Without a mirror, parallel trials quickly exhaust Docker Hub's anonymous pull limit. |
| `SREGYM_KIND_NODE_IMAGE` | Prebuilt node image used instead of building `kind/Dockerfile` in every trial. Defaults to `--kind-node-image`. |
| `SREGYM_EXTRA_CA_CERTS` | PEM bundle to trust in the sidecar and on every node, for TLS-inspecting egress proxies. Mount the file into the sidecar. |
| `SREGYM_ETCD_TMPFS_SIZE`, `SREGYM_DOCKER_STORAGE_DRIVER`, ... | The existing [DinD settings](../docker/dind/README.md). |

Example:

```yaml
services:
  sregym:
    environment:
      SREGYM_REGISTRY_MIRROR: https://registry.example.internal
```

### Cloud providers

A provider must run Docker Compose tasks and let the `privileged: true`
sidecar run its own Docker daemon and KIND cluster.

**Harbor itself does not get in the way.** It never rewrites or rejects a
service's `privileged`, `cgroup`, `security_opt` or `devices` keys; its
generated overrides only touch the `main` service. Whether the sidecar works
depends on what the provider's sandbox is.

The table below comes from reading Harbor's environment code (`main` on
2026-10-01). **None of these providers has been tested with SREGym yet.**

| Provider (`-e`) | Where Compose runs | Expected for the sidecar | Sizing notes |
|---|---|---|---|
| `ec2` | Real VM; Docker CE installed by Harbor | Should work (full kernel) | The default `m7i-flex.large` (2 vCPU, 8 GiB) is too small: set `instance_type` and `root_volume_size_gb`. |
| `gke` | Privileged `docker:dind` pod | Should work on GKE Standard | Autopilot blocks privileged pods. |
| `daytona` | DinD sandbox built from `docker:28.3.3-dind`. Daytona runs DinD sandboxes on Sysbox (per Daytona's issue tracker), which confines privileged nested containers to the sandbox. | Should work | CPU, memory and disk are passed through, except when `--ek dind_snapshot` is used. Your Daytona organization's sandbox limits must allow 8 CPUs and 16 GiB. |
| `prime`, `islo`, `tensorlake`, `blaxel`, `novita`, `vercel` | VM or microVM sandboxes | Likely | `vercel` caps disk at 32 GB: pass `--storage-mb 32768`. `blaxel` cannot set CPUs. `novita` sizes the sandbox from its template. `tensorlake` hosts may lack KVM, which makes them very slow. |
| `langsmith`, `hyperbrowser`, `runta` | Not stated in Harbor's code | Unknown | |
| `modal` | gVisor by default, with no bridge networking for Compose | No. The alpha `--ek modal_vm_runtime=true` microVM runtime is untested. | |
| `beam` | Forces host network, PID and cgroup on every service | Unlikely | |

On most providers, `allow_internet = false` isolates only the `main` service;
the sidecar keeps the registry access that problem setup needs. `islo`,
`tensorlake`, `prime`, `hyperbrowser` and `runta` apply network policy to the
whole sandbox instead.

Start every new provider with the self-test task:

```bash
uv run python -m sregym.harbor.adapter --self-test --output-dir datasets/sregym-selftest \
    --backend-image ghcr.io/sregym/sregym-dind:<tag> --kind-node-image ghcr.io/sregym/kind-node:v1.32.11-<tag>
export SREGYM_ORACLE_SECRET=$(cat datasets/sregym-selftest/.sregym-oracle-secret)
harbor run -p datasets/sregym-selftest -a oracle -e daytona
```

### Diagnosing a provider

The sidecar records what the host offers before it starts anything, in
`logs/dind/environment.txt` among the trial's collected artifacts:

- kernel and cgroup version, CPUs, memory and free disk
- whether the container is really privileged, and whether it runs in a user
  namespace or under Sysbox
- whether the kernel features SREGym uses are available (`br_netfilter`,
  `ipip` for Calico, `sch_netem` for Chaos Mesh delays, and others)
- inotify limits
- which registries and chart repositories are reachable

The same directory holds `dockerd.log`, plus `kind-logs/` when cluster creation
fails. The report's one-line summary also appears in the sidecar's log.

If setup fails before the backend starts, the sidecar marks the shared state
`failed` with the stage that broke. The healthcheck then stops at once with a
message such as:

```
SREGym sidecar setup failed during: private Docker daemon (exit 1). Diagnostics: /sregym-harbor/logs/dind
```

The sidecar then stays up for an hour so Harbor can still collect its
diagnostics.

## Validation status

Validated with Harbor 0.23.0 and Docker 29 on a 4-CPU, 16 GiB x86_64 VM.
That VM uses cgroup v1, and its egress policy blocks several registries.

| Check | Result |
|---|---|
| Self-test task, `-a oracle` | Reward 1.0 in 2m44s. The collect hook graded in the sidecar, and the separate verifier scored the collected grade. |
| Self-test task, `-a nop` | Reward 0.0 (`service_has_no_ready_endpoints`). |
| Agent isolation, probed from `main` during a trial | `kubectl` works through the proxy. The shared volume is read-only. The grading port is unreachable. Recovery without the token returns 403. There is no Docker socket, KIND nodes do not resolve, and the problem ID is not visible. |
| Sidecar exits during cluster setup | Harbor reports `HealthcheckError` about a minute later, not after the full hour. |
| Real problem (`network_policy_block`) | The Conductor path ran inside the sidecar: `fix_kubernetes`, mitigation-only stages, cleanup, deploy. Deployment then stopped because the VM blocks `registry.k8s.io`. The backend reported `failed` with the reason, Harbor reported `HealthcheckError`, and the backend logs were still collected. |

The setup-failure path was checked by running a generated task's Compose file
with a sidecar whose setup fails. Two cases were tried: a sidecar image without
`dockerd`, and the same sidecar with `privileged` removed. In both, the agent
container's `sregym-ready` failed within 5 seconds, naming the failing stage
(`private Docker daemon` or `cgroup delegation`). The held sidecar still served
`environment.txt` and `dockerd.log` to `docker compose cp`, which is how
artifacts are copied.

The **Harbor Self-Test** workflow (`.github/workflows/harbor-selftest.yml`)
runs the self-test through Harbor's local Docker environment on a GitHub-hosted
runner (cgroup v2, unrestricted network). It runs on changes to the Harbor
integration, and requires reward 1 from the oracle agent and 0 from the no-op
agent. On its first runs it passed: the oracle trial took about 3 minutes and
the no-op trial about 4. Each trial used the full four-node cluster.

The **Harbor Oracle Sweep** workflow (`.github/workflows/harbor-oracle.yml`,
manual) runs Harbor's oracle agent on real problems, one problem per runner.
Harbor requires the oracle to score 1.0 on every task before it accepts an
adapter. On 2026-10-01, **all 21 SREGym-Lite problems scored 1.0**. Six jobs
ran at a time, and each job took 10–21 minutes including the image build.
`network_policy_block`'s trial took 7m39s, covering deployment, Calico
NetworkPolicy fault injection, recovery and grading. The no-op agent check
(`check_nop`), which confirms each fault is live, and the full problem set
have not been run yet.

Run the sweep from the Actions tab: choose **Harbor Oracle Sweep** and enter
`sregym-lite`, or a list of problem IDs. Each job deploys a full application,
so keep `max_parallel` low enough to leave runners for other CI.

Not yet validated: runs on cloud providers, the problems beyond SREGym-Lite,
and real agents.

The VM used for the first checks above runs cgroup v1 and blocks several
registries. There, KIND's worker kubelets could not start inside the nested
daemon, so the self-test used a single-node cluster through a local Compose
overlay. Real problems need cgroup v2 and unrestricted registry access, as on
the GitHub runners above.

## Limitations

- **Mitigation only, by design.** The Harbor reward is the deterministic
  mitigation oracle. SREGym's LLM-judged diagnosis stage is not part of the port.
- **KIND-compatible problems only.** Problems that need real nodes are
  skipped. Some problems that are hard to run reliably on KIND, such as
  TrainTicket, are generated but may fail setup on small hosts. Validate
  individual tasks with the oracle agent before relying on them.
- **Loki is not deployed**, as in `main.py --use-external-harness`. Agents read
  logs with `kubectl logs`.
- **Setup cost.** A trial can spend 5–30 minutes deploying before the agent
  starts. The healthcheck allows up to an hour.

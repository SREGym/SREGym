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
   asks the sidecar to run the problem's own `recover_fault()`. Only the
   solution contains the token for this endpoint; the sidecar stores just its
   SHA-256.

The generated `instruction.md` never names the fault or problem ID. The problem
ID appears only in files that stay on the Harbor host: `task.toml` metadata and
the Compose file.

## Generate tasks

```bash
# Every eligible problem (121 of 125 today)
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

- problems that need Khaos
- problems that need a non-emulated cluster
- problems whose constructor queries a live cluster (currently `taint_no_toleration_social_network`)

Other options:

| Option | Default | Meaning |
|---|---|---|
| `--backend-image` | `ghcr.io/sregym/sregym-dind:latest` | Sidecar image. `SREGYM_HARBOR_IMAGE` overrides it when a task runs. |
| `--agent-timeout` | `1800` | Agent time limit in seconds, matching SREGym's runner. |
| `--cpus` / `--memory-mb` / `--storage-mb` | `8` / `16384` / `51200` | Resources requested for the whole task. Cloud providers size the sandbox from these. |
| `--limit`, `--overwrite` | | Standard Harbor adapter flags. |

Task names are `sregym/<problem-id>`, lowercased with `_` replaced by `-`.

## Build the backend image

The sidecar runs the DinD image with the SREGym checkout baked in, so rebuild
it whenever problems or oracles change:

```bash
git submodule update --init --recursive
python3 docker/dind/run.py build                  # tags sregym-dind:local
docker tag sregym-dind:local ghcr.io/sregym/sregym-dind:<version>
docker push ghcr.io/sregym/sregym-dind:<version>
```

Generate tasks with `--backend-image` pointing at the pushed tag, so a dataset
always pins the SREGym version that produced it. For local runs, either
generate with `--backend-image sregym-dind:local` or export
`SREGYM_HARBOR_IMAGE=sregym-dind:local`.

## Run with Harbor

```bash
uv tool install harbor            # 0.23 or newer

# Reference solution: every task should score 1.0
harbor run -p datasets/sregym-selftest -a oracle
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

Set these with a Compose overlay on the `sregym` service. Pass the overlay with
`harbor run --extra-docker-compose overlay.yaml`:

| Variable | Purpose |
|---|---|
| `SREGYM_REGISTRY_MIRROR` | Docker Hub pull-through mirror, e.g. `https://mirror.gcr.io`, for the private daemon and every KIND node. Parallel trials otherwise exhaust Docker Hub's anonymous pull limit quickly. |
| `SREGYM_KIND_NODE_IMAGE` | Use a prebuilt node image instead of building `kind/Dockerfile` in every trial. |
| `SREGYM_EXTRA_CA_CERTS` | PEM bundle to trust in the sidecar and on every node, for TLS-inspecting egress proxies. Mount the file into the sidecar. |
| `SREGYM_ETCD_TMPFS_SIZE`, `SREGYM_DOCKER_STORAGE_DRIVER`, ... | The existing [DinD settings](../docker/dind/README.md). |

Example:

```yaml
services:
  sregym:
    environment:
      SREGYM_REGISTRY_MIRROR: https://mirror.gcr.io
```

### Cloud providers

The task needs a provider that runs Docker Compose with `privileged: true`
services and lets that service run its own Docker daemon and KIND cluster.
According to Harbor's documentation:

- Compose runs natively on `docker`, `ec2` and `vercel`.
- Compose runs inside Docker-in-Docker on `daytona`, `gke` (Standard, not
  Autopilot), `modal` and others. These providers add another layer of nesting.
- Providers without Compose support cannot run these tasks.

Only local Docker has been exercised so far. Start with the self-test task on a
new provider before scheduling real problems.

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

Not yet validated: oracle and agent runs on real problems. These need
unrestricted registry access (`registry.k8s.io`, `quay.io`, `ghcr.io`,
`openebs.github.io` and other Helm repositories). On the cgroup v1 VM, KIND's
worker kubelets could not start inside the nested daemon, so the self-test ran
on a single-node cluster through a local Compose overlay. The four-node
runtime was validated on cgroup v2 hosts in the
[DinD guide](../docker/dind/README.md). Use cgroup v2 hosts for real problems.

## Limitations

- **Mitigation only, by design.** The Harbor reward is the deterministic
  mitigation oracle. SREGym's LLM-judged diagnosis stage is not part of the port.
- **KIND-compatible problems only.** Problems that need Khaos or real nodes are
  skipped. Some problems that are hard to run reliably on KIND, such as
  TrainTicket, are generated but may fail setup on small hosts. Validate
  individual tasks with the oracle agent before relying on them.
- **Loki is not deployed**, as in `main.py --use-external-harness`. Agents read
  logs with `kubectl logs`.
- **Setup cost.** A trial can spend 5–30 minutes deploying before the agent
  starts. The healthcheck allows up to an hour.

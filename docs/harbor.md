# Running SREGym on Harbor

[Harbor](https://harborframework.com) runs agent benchmarks as self-contained
tasks, locally or on cloud sandboxes, with many trials in parallel. SREGym can
generate a Harbor task for every problem that runs on its emulated cluster.

Each task is **one unprivileged container**. It holds a private four-node
Kubernetes cluster, the application, SREGym's grader and the agent. It needs no
`--privileged`, added capabilities, devices or Docker Compose, so it runs on
Harbor's cloud sandboxes, Daytona included, and can be published to Harbor Hub.

The integration targets Harbor 0.23 or newer (task `schema_version = "1.4"`).

## How a task works

```
  Harbor task container (unprivileged; root inside it, Docker's default capabilities)
  ┌───────────────────────────────────────────────────────────────────────────┐
  │ root                                                                      │
  │  k3s cluster: kind-control-plane, kind-worker, kind-worker2, kind-worker3  │
  │    each node in its own namespaces and cgroup (docker/userns-k3s)         │
  │  SREGym backend: deploys the problem, injects the fault, grades it        │
  │    127.0.0.1:16443 filtered Kubernetes API proxy                          │
  │    :8765 /oracle/recover (solution token)  127.0.0.1:8766 /grade (token)  │
  │                                                                           │
  │ agent (uid 1001)                                                          │
  │  kubectl ──► API proxy only. Cannot read the problem, the grader,         │
  │              the cluster's credentials or the grade token.                │
  └───────────────────────────────────────────────────────────────────────────┘
```

1. **Setup.** The task's healthcheck, `sregym-ready`, runs as root. Its first
   call starts `docker/harbor/start.sh` in the background, so no provider has to
   run the image's entrypoint. That script starts an image-pull proxy and the
   cluster, then `python -m sregym.harbor.backend`. The backend calls the
   Conductor's `start_problem()` as `main.py` does: it deploys the application,
   captures the oracle's baseline and injects the fault. Problems graded by
   Prometheus alerts (`AlertOracle`) first let the application run for 5
   minutes (`main.py --baseline 300`), so alerts that fire chronically on a
   small sandbox, such as CPU throttling on Astronomy Shop's Grafana, are
   already firing when the baseline is taken and are not blamed on the agent.
   The backend then starts SREGym's filtered Kubernetes API proxy and publishes
   the agent's kubeconfig.
2. **Readiness.** `sregym-ready` blocks until the fault is live and the agent's
   kubeconfig works, and fails at once if setup failed. Harbor starts the agent
   clock only after this.
3. **Agent.** Harbor runs the agent as the unprivileged `agent` user (task.toml
   `[agent] user`). It sees the application name, namespaces and description,
   the same information SREGym's own agents get, and works through `kubectl`
   against the proxy. The problem ID, SREGym's code, the cluster's admin
   credentials and the grading token are root-only.
4. **Grading.** The verifier runs in the same container as root
   (`environment_mode = "shared"`). It kills every process the agent left
   running, then asks the still-running backend to evaluate the problem's
   mitigation oracle. That oracle is the same object that captured the
   pre-fault baseline. `/grade` needs a token generated per trial and readable
   only by root, because the agent shares the backend's loopback. The reward is
   1.0 when the oracle succeeds and 0.0 otherwise. If the backend could not
   grade, no reward is written, so Harbor reports an error instead of a zero.
5. **Reference solution.** Harbor's oracle agent runs `solution/solve.sh`. It
   asks the backend to run the problem's own `recover_fault()`, then waits up
   to 10 minutes for the mitigation oracle to pass, as SREGym's problem
   validator does. Alert-based oracles keep failing for a few minutes after a
   fix, until rate-based alerts clear. The endpoint needs a per-task token: an HMAC of the task name keyed by the dataset's
   oracle secret. The task image carries only the token's SHA-256, root-only.
   The secret reaches `solve.sh` at run time through task.toml's `[solution]
   env`, which Harbor resolves for the oracle agent only.

   The adapter reads the secret from `SREGYM_ORACLE_SECRET`. If that is unset,
   it generates one and saves it in `.sregym-oracle-secret` at the dataset root,
   which `harbor publish` never uploads. Export it before running the oracle
   agent.

The generated `instruction.md` never names the fault or problem ID.

### The cluster

[docker/userns-k3s](../docker/userns-k3s/README.md) runs four k3s nodes inside
one container. Each node gets its own mount, network, PID, UTS, IPC and cgroup
namespaces and a cgroup subtree, joined by a bridge. The nodes use KIND's names,
so SREGym treats the cluster as its emulated one. Nodes have no route out; their
containerd pulls images through a proxy in the container.

The container's root must be able to create those namespaces and mount cgroups:

- **Sysbox sandboxes** (Daytona) and **VM sandboxes** allow this as they are.
  Under Sysbox the container already runs in a user namespace, so the cluster
  uses it directly instead of creating a nested one.
- **Plain Docker** (Harbor's `docker` environment) blocks it with its default
  seccomp profile and masked `/proc` paths. Run with the overlay below, which
  lifts only those two filters. On Ubuntu 24.04 hosts, also allow unprivileged
  user namespaces: `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`.

## Generate tasks

```bash
# Every eligible problem
uv run python -m sregym.harbor.adapter --output-dir datasets/sregym

# SREGym-Lite, or selected problems
uv run python -m sregym.harbor.adapter --suite sregym-lite --output-dir datasets/sregym-lite
uv run python -m sregym.harbor.adapter --task-ids network_policy_block incorrect_image --output-dir datasets/sregym

# A small check that a Harbor environment can run SREGym at all
uv run python -m sregym.harbor.adapter --self-test --output-dir datasets/sregym-selftest
```

No cluster is needed; problems are inspected against a placeholder kubeconfig.
The generator skips problems it cannot turn into working tasks and prints the
reason:

- problems that need a non-emulated cluster
- problems whose constructor queries a live cluster
- problems the k3s cluster cannot run (`K3S_UNSUPPORTED` in
  `sregym/harbor/adapter.py`): node faults injected with `docker exec` into KIND
  nodes, Calico-specific faults (the cluster runs flannel), a host-global sysctl
  and a replaced kube-proxy

| Option | Default | Meaning |
|---|---|---|
| `--backend-image` | `ghcr.io/sregym/sregym-harbor:latest` | Image each task builds on (`docker/harbor`). |
| `--agent-timeout` | `1800` | Agent time limit in seconds. |
| `--cpus` / `--memory-mb` / `--storage-mb` | `8` / `16384` / `51200` | Resources requested for the task container. Cloud providers size the sandbox from these. Daytona's default organization limits are 4 CPUs, 8 GiB and 10 GiB, which fit Hotel Reservation problems. |
| `--dataset-name` | `sregym/<suite>` | Harbor Hub dataset name used in the README. |
| `--limit`, `--overwrite` | | Standard Harbor adapter flags. |

Task names are `sregym/<problem-id>`, lowercased with `_` replaced by `-`.

## Build the image

The task image has the SREGym checkout baked in, so rebuild it whenever
problems or oracles change.

**For cloud providers**, run the **Publish Harbor Image** workflow
(`.github/workflows/publish-harbor-images.yml`, from the Actions tab). It pushes
`ghcr.io/sregym/sregym-harbor:<tag>` and prints the matching adapter command.
Cloud sandboxes pull anonymously, so the GHCR package must be public. Generate
tasks with `--backend-image` pointing at the published tag, so a dataset pins
the SREGym version that produced it.

**For local runs**:

```bash
git submodule update --init --recursive
docker build -f docker/harbor/Dockerfile -t sregym-harbor:local .
```

Then generate with `--backend-image sregym-harbor:local`.

## Run with Harbor

```bash
uv tool install harbor            # 0.23 or newer

# Reference solution: every task should score 1.0. The oracle agent needs
# the secret the adapter saved beside the tasks.
export SREGYM_ORACLE_SECRET=$(cat datasets/sregym/.sregym-oracle-secret)
harbor run -y -p datasets/sregym/network-policy-block -a oracle -e daytona

# No-op agent: every task should score 0.0
harbor run -y -p datasets/sregym/network-policy-block -a nop -e daytona

# A real agent, several tasks at a time
harbor run -y -p datasets/sregym-lite -a claude-code -m anthropic/claude-sonnet-5 -e daytona -n 4
```

`-y` confirms passing `SREGYM_ORACLE_SECRET` to the reference solution.

**Harbor's local Docker environment** needs the overlay described above:

```bash
harbor run -y -p datasets/sregym/network-policy-block -a oracle -e docker \
    --extra-docker-compose "$PWD/docker/harbor/local-docker.yaml" --cpus ignore --memory ignore
```

Pass the overlay as an absolute path. On a host with fewer CPUs than the task
requests, keep `--cpus ignore --memory ignore` or use `--override-cpus N`.

### Diagnosing a trial

Every trial collects `/sregym-harbor/logs` as an artifact:

- `start.log`: cluster startup and the backend handoff
- `cluster.log`: the node fabric; each node's own log stays in the container
- `sregym_*.log`: the backend, including deployment and grading

If setup fails before the backend starts, `start.sh` marks the shared state
`failed` with the stage that broke. The healthcheck then stops at once with a
message such as:

```
SREGym setup failed during: cluster. Log: /sregym-harbor/logs/start.log
```

## Publish to Harbor Hub

[Harbor Hub](https://hub.harborframework.com) works like PyPI: datasets are
published from the CLI, and anyone with access can then run them with
`harbor run -d sregym/<dataset>`. Publish only tasks that scored 1.0 in an
oracle sweep.

1. **Publish the image.** Run the **Publish Harbor Image** workflow, and make
   the `sregym-harbor` GHCR package public the first time.
2. **Generate the dataset** with the published tag. Keep the oracle secret: you
   need it to run the oracle agent against the published dataset, and it never
   leaves your machine.

   ```bash
   git submodule update --init --recursive
   export SREGYM_ORACLE_SECRET=...   # optional: omit to generate one in the dataset directory
   uv run python -m sregym.harbor.adapter --suite sregym-lite --output-dir datasets/sregym-lite \
       --backend-image ghcr.io/sregym/sregym-harbor:<tag>
   ```

   The adapter writes the dataset `README.md` that the Hub shows, plus a
   `README.md` for each task.
3. **Create the manifest.** `dataset init` adds every task in the directory and
   keeps the generated README:

   ```bash
   cd datasets/sregym-lite
   harbor dataset init sregym/sregym-lite --author "SREGym Team" \
       --description "Curated live Kubernetes incidents for AI SRE agents, graded by mitigation oracles."
   ```
4. **Publish.** Packages are private unless you pass `--public`:

   ```bash
   harbor auth login
   harbor publish . -t v1.0            # private
   harbor publish . -t v1.0 --public   # or public
   ```

   The first publish under `sregym` creates the organization, with you as its
   owner; add co-maintainers on the Hub. If someone else already owns `sregym`,
   publishing fails with a permission error. In that case, ask the Harbor team.
5. **Check it:**

   ```bash
   SREGYM_ORACLE_SECRET=... harbor run -y -d sregym/sregym-lite@v1.0 -a oracle -e daytona -l 1
   ```

To publish an update, regenerate with a new image tag and the same secret, run
`harbor sync`, then publish with a new tag. A leaderboard can then be added with
`harbor hub leaderboard init --package sregym/sregym-lite`. See Harbor's
[leaderboard guide](https://harborframework.com/docs/core-concepts/harbor-hub/leaderboards).

## Validation status

| Check | Result |
|---|---|
| `network_policy_block`, oracle agent, Harbor on Daytona (4 CPUs, 8 GiB, 10 GiB) | Reward 1.0 in 6m43s |
| `network_policy_block`, no-op agent, Harbor on Daytona | Reward 0.0 (`fault_still_present`) in 4m47s |
| `network_policy_block` and `service_wrong_pod_selection_hotel_reservation`, SREGym's lifecycle validator in a Daytona sandbox | Both passed: deploy, inject, oracle fails, recover, oracle passes |
| Agent isolation in a ready Daytona sandbox, probed as `agent` | Problem ID, SREGym code, cluster credentials, grade token and logs unreadable. `/grade` and `/oracle/recover` refuse without tokens. The k3s API refuses without credentials. `kubectl` works through the proxy. |
| Self-test, oracle and no-op agents, **Harbor Self-Test** workflow (`docker` environment, GitHub runner) | Rewards 1 and 0 |

Not yet validated: problems beyond those above on Daytona, other cloud
providers, real agents, and arm64 hosts (on an arm64 Mac, the Prometheus stack
did not become ready within the hour).

The **Harbor Oracle Sweep** workflow (`.github/workflows/harbor-oracle.yml`,
manual) runs Harbor's oracle agent on real problems, one problem per runner,
with the local Docker environment.

## Limitations

- **Mitigation only, by design.** The Harbor reward is the deterministic
  mitigation oracle. SREGym's LLM-judged diagnosis stage is not part of the port.
- **k3s-compatible problems only.** Problems that need real nodes, KIND node
  containers or Calico are skipped. Validate individual tasks with the oracle
  agent before relying on them.
- **The agent shares the container's kernel and process table** with the
  cluster and grader. File permissions and tokens keep it away from the grader;
  it can see process names. Harbor's sandbox, not the task, isolates trials
  from each other and from the host.
- **Loki is not deployed**, as in `main.py --use-external-harness`. Agents read
  logs with `kubectl logs`.
- **Setup cost.** A trial spends several minutes deploying before the agent
  starts. The healthcheck allows up to an hour.

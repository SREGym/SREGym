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
  │ agent (uid 48713)                                                         │
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
   Their oracle also ignores alerts about workloads the proxy hides from the
   agent, SREGym's load generators (`AlertOracle(ignore_hidden_workloads=True)`):
   the agent cannot see or fix them, and a replaced load-generator pod's alerts
   escape the baseline. The backend then polls the mitigation oracle until it
   fails, as SREGym's problem validator does: alerts fire minutes after
   injection, and an agent that stopped before then would pass. It waits up to
   10 minutes, twice the validator's default, because on a 4-CPU sandbox some
   alerts took longer. If the oracle has not seen the fault by then, setup
   fails and Harbor reports an error rather than a reward. Last, the backend starts SREGym's
   filtered Kubernetes API proxy and publishes the agent's kubeconfig.
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
so SREGym treats the cluster as its emulated one, and as in KIND the control
plane is tainted, so workloads run on the three workers. Nodes have no route
out; their containerd pulls images through a proxy in the container.

The container's root must be able to create those namespaces and mount cgroups:

- **Sysbox sandboxes** (Daytona) and **VM sandboxes** allow this as they are.
  Under Sysbox the container already runs in a user namespace, so the cluster
  uses it directly instead of creating a nested one.
- **Plain Docker** (Harbor's `docker` environment) blocks it with its default
  seccomp profile and masked `/proc` paths. Run with the overlay below, which
  lifts only those two filters and raises the open-file limit (TiDB's TiKV
  needs about 82920 open files). On Ubuntu 24.04 hosts, also allow unprivileged
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
  nodes, Calico-specific faults (the cluster runs flannel), a host-global sysctl,
  a replaced kube-proxy, and an admission-webhook fault that needs denied
  connections to hang. k3s enforces NetworkPolicy with kube-router, which
  rejects them at once (`REJECT --reject-with icmp-port-unreachable`), where
  Calico drops them.

These 13 problems are generated but are not reliable as tasks at the default
size (4 CPUs, 8 GiB) on Daytona. Leave them out of a dataset (`--task-ids`). A
failed setup is reported as an error, never as a reward. The runs are the three
sweeps under [Validation status](#validation-status); the first used a 5-minute
fault wait, the others 10 minutes.

| Problem | Oracle agent, 3 sweeps | Why |
|---|---|---|
| `gc_capacity_degradation` | 1.0 in all 5 runs that got through setup (no-op 0.0 in all 3); setup failed in 5 of 10 | its alerts had not fired within 10 minutes 3 times, also when it ran alone; Blueprint Hotel Reservation once did not become ready while some 30 sandboxes ran at once; once the cgroup error below |
| `astronomy_shop_product_catalog_service_failure` | 1.0, 1.0; setup failed once (5-minute wait) | passes when set up, but its alerts did not fire in time in 2 of 3 no-op runs |
| `astronomy_shop_ad_service_failure` | 1.0 once, 0.0 twice | Locust's `HighRequestErrorRate` can keep firing for 10 minutes after recovery |
| `astronomy_shop_ad_service_high_cpu` | 1.0, 0.0, setup failed once | its alerts are slow to fire and to clear |
| `astronomy_shop_cart_service_failure` | 0.0; setup failed twice | as above |
| `astronomy_shop_ad_service_manual_gc` | setup failed 3 of 3 | its alerts rarely fire within the wait |
| `astronomy_shop_ad_service_image_slow_load` | setup failed 3 of 3 | no alert fires. SREGym runs the load generator without browser traffic, so slow images are probably never requested. |
| `kafka_queue_problems` | 0.0 twice, setup failed once | `MessageConsumerLag` keeps firing after recovery |
| `loadgenerator_flood_homepage` | setup failed 3 of 3 | the flood's only alert is CPU throttling on the load generator, which the agent cannot see, so no alert it could act on fires |
| `capacity_decrease_rpc_retry_storm`, `load_spike_rpc_retry_storm` | setup failed every time | CPU stress on every node starves the API server and Prometheus |
| `trainticket_f17_nested_sql_select_clause_error`, `trainticket_f22_sql_column_name_mismatch_error` | setup failed every time | Train Ticket's dozens of services overload the sandbox |

| Option | Default | Meaning |
|---|---|---|
| `--backend-image` | `ghcr.io/sregym/sregym-harbor:latest` | Image each task builds on (`docker/harbor`). |
| `--agent-timeout` | `1800` | Agent time limit in seconds. |
| `--cpus` / `--memory-mb` / `--storage-mb` | `4` / `8192` / `10240` | Resources requested for the task container. Cloud providers size the sandbox from these. The defaults are Daytona's default per-sandbox limits, which refuse anything larger, and every problem was validated at this size. |
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
| **SREGym-Lite on the published image**, oracle agent, Harbor on Daytona, `ghcr.io/sregym/sregym-harbor:bcb224d85322`, generated exactly as for the Hub | **17/17 at 1.0** |
| **Oracle sweep of every generated task**, Harbor on Daytona, default task size (4 CPUs, 8 GiB, 10 GiB), image `ae0f9edc14db` with every later change up to `d4ef5df` layered on (the same code as `bcb224d85322`) | **80 of 93 pass**: 75 of the 77 tasks not graded by alerts scored 1.0 (not the two Train Ticket problems), and 5 alert-graded tasks scored 1.0 here and in both earlier sweeps. Every trial that reached the agent had first shown the oracle failing with the fault live. The other 12 are in the table under [Generate tasks](#generate-tasks). |
| No-op agent, the 16 alert-graded tasks plus `operator_wrong_operator_image`, three sweeps | 0.0 in every run that got through setup (28 runs). The rest failed setup because no alert fired within the wait, and Harbor scored them as errors, not rewards. Before the fault wait, the no-op agent scored 1.0 on `gc_capacity_degradation` and `loadgenerator_flood_homepage`. |
| Social Network's 14 tasks with the restored multi-arch nginx-thrift chart, oracle agent, Harbor on Daytona | 14/14 at 1.0 |
| `gc_capacity_degradation`, oracle agent, twice with the control-plane taint (`bcb224d85322`) and twice without | 1.0 once each. The other runs failed setup: with the taint its alerts were late, without it the cluster hit the cgroup error below. The taint does not affect it. |
| Two earlier sweeps of every generated task: image `3946472e669d` without the fault wait, and `ae0f9edc14db` with a 5-minute wait and no control-plane taint | 86 of 94 at 1.0 after reruns in the first. Without the wait, a task whose oracle never saw its fault also scored 1.0, which is how `operator_wrong_operator_image` (its oracle then checked the wrong namespace), `cumulative_admission_webhook_timeout_hotel_reservation` and some Astronomy Shop alert tasks passed. 80 of 94 in the second. |
| The first 7 problems in `K3S_UNSUPPORTED`, generated by hand and run with the oracle agent on Daytona, image `2610c6277410` | None works as a task, for the listed reasons. Five fail in setup: no Calico CRDs, no KIND node containers or Ansible inventory, emulated cluster refused, host-global sysctl, no kube-proxy DaemonSet. `kubelet_crash` finds no kubelet to crash (0.0). `pod_cidr_exhaustion_hotel_reservation`'s Calico IPPool is rejected, so its fault never takes effect: the oracle and no-op agents both score 1.0. |
| `cumulative_admission_webhook_timeout_hotel_reservation` (now in `K3S_UNSUPPORTED`), oracle and no-op agents, and a live Daytona sandbox | Setup failed in all 6 runs: the oracle never saw the fault. In the sandbox, a connection from the API server's node to an isolated webhook backend was refused at once by kube-router's `REJECT` rule, so the webhook calls never time out. |
| SREGym-Lite plus `taint_no_toleration_social_network`, oracle agent, Harbor on Daytona, image `2610c6277410` (after merging `main` at #1077) | 18/18 at 1.0 |
| SREGym-Lite, oracle and no-op agents, **Harbor Oracle Sweep** workflow (`docker` environment, GitHub runners) | 17/17 at 1.0 with the oracle agent, 0.0 with the no-op agent |
| `network_policy_block`, no-op agent, Harbor on Daytona | Reward 0.0 (`fault_still_present`) |
| Agent isolation in a ready Daytona sandbox, probed as `agent` | Problem ID, SREGym code, cluster credentials, grade token and logs unreadable. `/grade` and `/oracle/recover` refuse without tokens. The k3s API refuses without credentials. `kubectl` works through the proxy. |
| Self-test, oracle and no-op agents, **Harbor Self-Test** workflow (`docker` environment, GitHub runner) | Rewards 1 and 0 |
| `operator_overload_replicas` (FleetCast), **Harbor Oracle Sweep** (`docker` environment, GitHub amd64 runner) | 1.0 once the overlay raised the open-file limit; before, TiKV exited at start |

`kafka_producer_leak` and `postgres_lock_contention_product_catalog`, flaky in
the first sweep (2 of 3 and 1 of 2), scored 1.0 in both later ones.

About 1 in 70 cluster starts on Daytona fails at once: moving the first node's
`unshare` parent into its cgroup returns EIO (`cluster.log` names the node and
the cgroup state). Harbor reports a setup error, not a reward; rerun the trial,
e.g. with `harbor job resume --filter-error-type HealthcheckError`.

**arm64.** The image builds and the cluster runs on arm64 Linux (GitHub's
`ubuntu-24.04-arm` runners, **Harbor Oracle Sweep** with `runner:
ubuntu-24.04-arm`). `network_policy_block` (Hotel Reservation) and
`env_variable_shadowing_astronomy_shop` scored 1.0 there, Prometheus stack
included. Social Network and FleetCast failed there on amd64-only images, both
now replaced but not yet rerun on arm64. Social Network's chart pinned
nginx-thrift to `yg397/openresty-thrift:xenial`: SREGym-applications#9 had
moved it to the multi-arch `ghcr.io/sregym/openresty-thrift`, and the merge of
#10 brought the old image back. The submodule now points at a commit (on #13's
branch) that restores it. FleetCast's TiDB cluster used the
operator's default helper image `busybox:1.26.2`, also amd64-only; SREGym now
sets `spec.helper.image` to the multi-arch `busybox:1.36`. With it, FleetCast
still scores 1.0 on Daytona (amd64); it has not been rerun on arm64. On an arm64 Mac
(Docker Desktop) the Prometheus stack once did not become ready within the
hour; that did not recur on arm64 Linux.

Not yet validated: other cloud providers and real agents.

The **Harbor Oracle Sweep** workflow (`.github/workflows/harbor-oracle.yml`,
manual) runs Harbor's oracle agent on real problems, one problem per runner,
with the local Docker environment.

## Limitations

- **Mitigation only, by design.** The Harbor reward is the deterministic
  mitigation oracle. SREGym's LLM-judged diagnosis stage is not part of the port.
- **k3s-compatible problems only.** Problems that need real nodes, KIND node
  containers or Calico are skipped. Validate individual tasks with the oracle
  agent before relying on them.
- **Sized for a 4-CPU, 8 GiB sandbox.** 13 generated problems are not reliable
  at that size: they overload the sandbox, or their alerts do not fire or do
  not clear in time. See [Generate tasks](#generate-tasks).
- **Alert-graded problems are graded when the agent stops.** `AlertOracle`
  fails while any new alert fires in the namespace, and alerts lag a fix by a
  few minutes. The reference solution waits for the oracle to pass; an agent
  that stops right after a correct fix can still score 0, as in SREGym's own
  runs.
- **The agent shares the container's kernel and process table** with the
  cluster and grader. File permissions and tokens keep it away from the grader;
  it can see process names. Its UID (48713) is one no workload runs as, so it
  cannot signal pod processes. Harbor's sandbox, not the task, isolates trials
  from each other and from the host.
- **Loki is not deployed**, as in `main.py --use-external-harness`. Agents read
  logs with `kubectl logs`.
- **Setup cost.** A trial spends several minutes deploying before the agent
  starts. Alert-graded problems add five minutes of steady state, then a few
  more until their alerts fire. The healthcheck allows up to an hour.

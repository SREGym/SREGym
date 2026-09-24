# SREGym in Docker-in-Docker (experimental)

Each outer container owns a Docker daemon, the existing four-node KIND/Calico
cluster, SREGym, and its nested agent containers. Run one problem per outer
container for parallel evaluation. Fixed ports, cluster names, the baseline
cache, application metadata and agent build directories are private to each run.
The daemon and benchmark share a filesystem, so nested agent bind mounts work
without translating host paths. No host Docker socket or host network is used.

DinD builds the node image from `kindest/node:v1.32.11`, with SREGym's udev/socat
additions. This contains containerd 2.2.0; the older custom images contain 2.0.2
and exhibited CNI startup hangs during validation. Override the base with
`SREGYM_KIND_BASE_IMAGE` in the env file when testing another compatible node image.
The existing host KIND setup retains its default image unless `KIND_NODE_IMAGE`
is explicitly set.

The disposable etcd database uses a 512 MiB tmpfs limit per run to avoid API
timeouts when concurrent image extraction saturates the host disk. This memory
counts toward the outer container's limit. Application volumes remain on disk.
Set `SREGYM_ETCD_TMPFS_SIZE=0` in the env file to use disk-backed etcd, or change
the size for larger campaigns. Do not use tmpfs mode to evaluate etcd disk faults
or persistence behavior.

## Requirements

Use a Linux Docker host with the Buildx plugin (or a Linux VM behind Docker Desktop) that permits
privileged containers and writable cgroups. Start with **8 CPUs and 16 GiB RAM
per concurrent SREGym-Lite run**, plus disk space for each daemon's image cache.
Larger problems need more resources. This retains KIND's problem limitations;
it does not add support for problems requiring real machines or Khaos.

Configure the host's inotify limits as described in the [KIND guide](../../kind/README.md).
These limits are shared across containers, so parallel runs may require higher
values. Privileged DinD is intended for trusted benchmarking infrastructure;
outer containers share the host kernel, and host-wide OS faults are not isolated
like they would be in separate VMs. Agent containers still use SREGym's existing
isolation and proxy configuration inside the private daemon.

Some application images, including the current HotelReservation image, are
AMD64-only. On ARM64, those workloads additionally require host binfmt/QEMU
emulation or rebuilt ARM64 application images. The outer runtime and KIND nodes
run natively on ARM64; DinD does not itself translate application binaries.

## Build and run

From the repository root:

```bash
git submodule update --init --recursive
python3 docker/dind/run.py build

# No LLM credentials: cluster, mounts, networking, workload and recovery checks.
python3 docker/dind/run.py run -- bash docker/dind/smoke.sh

# Uses OPENAI_API_KEY from the host environment (also supports Anthropic,
# Google and AWS environment credentials; see run.py for the allowlist).
python3 docker/dind/run.py run --name sregym-image -- \
  uv run --frozen main.py --problem incorrect_image --agent stratus --model gpt-5
```

Use `run --env-file /path/to/credentials.env` for other environment settings.
Credential files must stay outside the image. Host login directories are not
automatically mounted. The image build excludes common secret and output paths;
review your checkout for other private files before building.

Results and daemon logs survive container removal in `results/dind/<run-name>/`.
Set `--output /absolute/path` to change this; always use a distinct directory for
each concurrent run. Files are written by container root. Docker data uses a
private anonymous volume removed by `docker run --rm`, so images are downloaded
again on each run. Initial cluster and agent-image startup can take several minutes.

Run two smoke checks simultaneously to exercise isolation (both create the same
cluster name, namespace and local listener port):

```bash
python3 docker/dind/run.py run --name sregym-check-a -- bash docker/dind/smoke.sh > /tmp/sregym-a.log 2>&1 &
first=$!
python3 docker/dind/run.py run --name sregym-check-b -- bash docker/dind/smoke.sh > /tmp/sregym-b.log 2>&1 &
second=$!
wait "$first"; first_status=$?
wait "$second"; second_status=$?
test "$first_status" -eq 0 && test "$second_status" -eq 0
```

Replace each smoke command with a different `main.py --problem ...` command to
run problems concurrently. Each `main.py` campaign remains serial internally.
Use `--cpus` and `--memory` before `--` to set outer container limits.

For disposable evaluations on a slow disk, `--docker-tmpfs-size 24g --memory 32g`
puts the entire private Docker data directory in memory, using a sparse ext4
loop image in tmpfs so image-layer extended attributes work on older kernels.
This requires loop-device support on the host. The tmpfs limit counts
toward the outer memory limit, so leave room for Kubernetes, applications and the
agent. Results still persist on the host. This option changes all nested storage
behavior: use disk-backed runs for disk faults, persistence or I/O measurements.

To keep an environment available, omit the command and use a second terminal:

```bash
python3 docker/dind/run.py run --name sregym-dev
# Wait until this succeeds before issuing cluster commands:
docker exec sregym-dev test -f /run/sregym-ready
docker exec -it sregym-dev bash
docker stop sregym-dev
```

The entrypoint starts the daemon, waits for Docker, creates the cluster, waits
for Calico/nodes, then starts the requested command. It propagates command exit
status, handles termination and shuts down the daemon. Startup failures leave
diagnostics under the run's `dind/` directory. An existing Docker socket is rejected.
On cgroup v2 it first moves its processes into an `init` child group and delegates
controllers to nested containers; skipping this makes nested systemd fail with
`Failed to create /init.scope control group: Structure needs cleaning`. The launcher
explicitly requests a private cgroup namespace. Failed KIND nodes are retained
until their logs have been exported, then removed with the outer environment.
If overlay2 is unavailable on the backing filesystem, try setting
`SREGYM_DOCKER_STORAGE_DRIVER=vfs` via the env file (slower and larger).

## Harbor integration boundary

[Harbor's Docker environment](https://www.harborframework.com/docs/tasks) supports
Compose definitions in `environment/docker-compose.yaml`. A privileged service
using this image can supply the SREGym backend for a task. Its entrypoint must be
preserved, and callers must wait for `/run/sregym-ready` before starting evaluation.
Providers must explicitly support privileged nested Docker; a generic sandbox
that only accepts a Dockerfile is not sufficient.

This adds the container runtime building block, **not a Harbor task adapter**.
A complete adapter still needs task instructions, fault initialization, agent
access through SREGym's proxies, and a verifier mapping SREGym's oracle results
to Harbor rewards. Do not give a Harbor agent a root shell in this backend:
it contains benchmark definitions and grading code. Keep the agent in a separate
container, as the existing SREGym runner does.

Prior work inspected while implementing this runtime:

- [SREGym's Harbor k3s adapter](https://github.com/SREGym/harbor/tree/039df85ded8a4a61a1826bc882f0a09868800f6b/adapters/sregym)
  provides task generation, readiness checks and mitigation-oracle reward mapping.
  Its single-node k3s backend excludes multi-node problems. Its generated solution
  script calls `run-oracle.py`, which evaluates rather than repairs a fault in the
  current SREGym checkout; recovery validation must call `recover_fault()` first.
- [The terminal-bench k3d task](https://github.com/SREGym/terminal-bench/tree/main/tasks/k8s-target-port-misconfiguration)
  mounts the host Docker socket and reuses fixed cluster names. That startup and
  deletion behavior needs private daemons before it can safely run concurrently.

## Validation status

Run real problem lifecycles without API keys using the existing validator:

```bash
python3 docker/dind/run.py run --name sregym-network-policy -- \
  python tests/integration/validate_problem.py --problem network_policy_block \
  --summary results/network-policy-validation.md
```

This deploys the application, injects the fault, requires the mitigation oracle
to report failure, calls `recover_fault()`, and requires the oracle to report success.
For a noop agent run through the complete benchmark/agent-container path:

```bash
python3 docker/dind/run.py run --name sregym-noop -- \
  python main.py --problem wrong_service_selector_hotel_reservation \
  --agent autosubmit --stages mitigation
```

The noop run should complete with mitigation failure because the agent does not
repair the fault. A zero process exit status alone is not evidence of successful
recovery; inspect the oracle result and phase artifacts.

Host-only tests: `python3 -m unittest discover -s tests/dind -v`.
The live smoke script covers four ready nodes, nested bind mounts and host
networking, service connectivity, and deployment scale-down/recovery.

Live validation on an Ubuntu 22.04 ARM64 host used Docker 29.1.3, cgroup v2,
8 CPUs/16 GiB per disk-backed environment, and QEMU for AMD64-only application images.
Two concurrent environments each reached four Ready nodes with distinct Docker
daemon IDs and Kubernetes namespace UIDs; both passed the smoke script. One
environment was used to debug startup; the other started directly from the
image with no manual cluster changes. A third environment passed the smoke test
with memory-backed ext4 Docker storage. Its data limit was increased from 20 to
24 GiB, with a 32 GiB container memory limit. The nine launcher tests and six
campaign/publication/judge-preflight tests passed.

Real oracle lifecycles passed for `wrong_service_selector_hotel_reservation`
(disk-backed) and `network_policy_block` (memory-backed). Both detected the
injected fault and confirmed recovery. This host's slow disk caused the initial
600-second selector deployment and a separate 1800-second network-policy
deployment to time out; those failed attempts were retained alongside the
successful validation reports. Memory-backed storage is useful here, but it
changes storage behavior and is not a substitute for validating disk faults.

The DinD image defaults to `CALICO_TIMEOUT=600s` and
`WAIT_FOR_POD_READY_TIMEOUT=1800` to allow cold image downloads and extraction.
Override these through an env file if needed. These extend waits without bypassing
readiness checks; the existing host setup keeps its original defaults.
Mitigation-only campaigns skip the unused diagnosis-judge API preflight.
Temporary CSVs and opaque agent artifacts are staged on the results filesystem,
so final publication also works when `results/` is a bind mount.
The full `autosubmit --stages mitigation` campaign completed with
`run_status=complete`, the expected `Mitigation.success=False`, published agent
artifacts, and per-problem/aggregate CSVs. This included the filtered agent
container, API submission, executable oracle and teardown, without API keys.

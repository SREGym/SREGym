# Verifier isolation

Every conductor run executes diagnosis and mitigation grading in a separate
Docker container. Standalone mitigation grading uses the same runtime. Host
grading cannot be selected. The evaluated agent receives neither the verifier image nor its input
channel. The verifier has no Docker socket, host filesystem mounts, published
ports, host process namespace, or host networking. It runs as UID 10001 with a
read-only root filesystem, all Linux capabilities dropped, no-new-privileges,
and bounded CPU, memory, process count, scratch space, output, and execution time.

The first run builds an image from the current source and locked dependencies.
The image uses the runner's exact final CPython version because its state
handoff uses cloudpickle. Both that version and the source bytes enter the
image cache key. An unavailable base image or incompatible frozen dependency
stops preparation before fault injection; a newer Python version is not
implicitly qualified by the repository's open-ended Python requirement.
The runner supplies a tar context containing tracked source, initialized
application submodule files, and the verifier implementation. Untracked files,
symlinks, private dotfiles, Git data, virtual environments and runtime artifacts
are excluded. The invocation pins
the resulting Docker image ID, rather than relying on a mutable tag. Image
preparation happens before injection and before the agent stage opens.

The standard DinD build launcher supplies a separate, allowlisted source context
with a path and SHA256 manifest. Packaged runtimes without Git validate that
manifest before building their verifier image; they never recursively discover
extra runtime files. The source ordering is identical in checkouts and packaged
images, so identical inputs reuse the same image cache key. Use the documented
`python3 docker/dind/run.py build` entrypoint to prepare this named build context.

## State and results

The existing healthy baseline capture still happens before injection. At grading,
the owning runner snapshots the **live oracle object**, including its compound
children, captured replica/alert baselines, problem configuration, and expected
state. The worker does not construct a new Problem or recapture the baseline.

Trusted state and flattened Kubernetes credentials travel over Docker stdin.
All oracle output, including output inherited by subprocesses, goes to a private,
bounded log outside agent mounts. Only the worker's protocol writes to its saved
stdout channel. The runner requires a matching invocation ID, a boolean verdict,
a clean process exit, and no extra output. Stale, malformed, missing, crashed,
cancelled or timed-out invocations cannot become passes. A container failure is
reported as `harness_error`; the runner never falls back to host grading.

Kubernetes clients are rebuilt in the container using the private verifier
kubeconfig. Locks and events are recreated while retaining identity and event
state. Unknown live threads and processes require an explicit state adapter and
fail serialization. Only handles irrelevant to grading may be declared in
`Problem.verifier_excluded_fields`.

Runner-owned cart traffic is drained on its owning host before the acknowledged
ledger is snapshotted, matching the oracle's existing drain behavior. The search
task's active host workload remains running. Its metrics, sample, start, stop,
and rate calls use a narrow request/response protocol over the same private
Docker pipes. Those adapters perform workload IO, not grading, and expose no
network service or arbitrary method execution.

Diagnosis judging uses the same boundary. Its checkpoint, expected answer,
checklist, parsing and scoring execute in the verifier. Provider requests use a
narrow model-inference adapter over the private pipe so existing API and CLI
judge backends keep their host-side credentials and connectivity. The adapter
returns model content, never a grade. Transport failures cannot fall back to
host evaluation.

## Kubernetes access and compatibility

The verifier runs beside the workload cluster on the existing Docker host;
this change does not introduce a VM or remove existing repair operations.
It has its own mount and process namespaces, private stdin/stdout, and no
listening service. Read-only files prevent writes from within the verifier;
they do not make files confidential from the Docker host administrator.

The security assumption is that the evaluated agent cannot control the Docker
host, its daemon, or the trusted runner process. Existing rootful Kind nodes
are privileged containers sharing that host's kernel and devices. Unrestricted
node-root access therefore exceeds this assumption: a sibling container alone
cannot guarantee confidentiality or integrity against host compromise. Nor
does it protect the runner from an agent launched as the same unrestricted
host user. These are unresolved deployment limits, not protections supplied by
`--read-only`. Rootless Kind changes this boundary but also changes supported
operations, including storage and host-level clock control; it is not enabled
implicitly by this migration.

On native Linux with Kind, the runner matches the host API port to the
actual control-plane container, joins its Docker bridge, and preserves TLS
verification. Externally reachable HTTPS API servers use Docker's bridge network.
Other loopback endpoints must be replaced with a reachable private kubeconfig.
Kubeconfigs that disable TLS certificate verification are rejected.

Use `--verifier-kubeconfig /private/verifier.kubeconfig` for dedicated credentials
with the permissions required by the selected oracles. Some existing checks
create probe pods, restart workloads, or read node configuration, so read-only
RBAC is not sufficient for every problem. Kind node checks use Kubernetes probes
in the protected `sregym-verifier` namespace, which the existing agent visibility
policy excludes from direct reads, mutations, logs, exec, lists and watches.
Prometheus and Promtail also drop this namespace from agent-facing telemetry.
The probes are deleted after use; they need no verifier Docker socket. Do not supply the evaluated
agent's filtered proxy kubeconfig. Exec/auth-provider credentials are materialized
on their owning host and refreshed before grading; the plugin and its cloud
environment are not transported. Credentials must remain valid for the grading
budget. Provider/model credentials are never forwarded to the verifier.

`ConductorConfig` requires container verification for embedded callers too.
`verifier_isolation=False` is rejected. Adapters must preserve the owning
conductor or transport a private snapshot containing the captured baseline.
The proposed Harbor adapter in PR #1047 still needs integration validation:
its persistent Conductor session can preserve live state through this runtime,
but its self-test, verifier shutdown and infrastructure-error propagation also
need migration. Converting a host-produced verdict in a reward container is
insufficient. The separate `--use-external-harness` CLI exits after injection;
that lifecycle cannot preserve live workload handles for later grading. It
needs a persistent owning session before claiming equivalent stateful runs.

## Standalone grading

`run-oracle.py --problem ...` now executes in the verifier container. It retains
its existing limitation: reconstructing a problem cannot recover a previous
baseline. For baseline-preserving standalone verification, the owning runner can save its live oracle
after baseline capture and injection:

```python
from pathlib import Path
from sregym.service.verifier_state import save_oracle_snapshot

save_oracle_snapshot(problem.mitigation_oracle, Path("/private/run/oracle.pickle"))
```

Then run `python run-oracle.py --verifier-snapshot /private/run/oracle.pickle`.
Snapshots must be private, runner-owned regular files. They contain trusted
Python state, may include expected answers, and must **never** come from an agent
or be placed in agent-accessible directories. The helper creates the file with
mode 0600 and refuses to replace an existing snapshot. Tasks with live host
workload handles use the owning conductor's pipe instead of this standalone path.
The CLI validates and reads a single no-follow file descriptor, with a bounded
read, so replacing a pathname after its metadata check cannot substitute input.

## Scope

This change protects grading execution and state. It does not change existing
oracle acceptance criteria or guarantee the absence of reward hacking. A weak
oracle can still reward a fabricated application response or missing telemetry;
independent data and behavioral invariants remain necessary for new fault tasks.
Agent prompts, operational evidence, and application manifests are unchanged.
Full LLM evaluation campaigns have not been established by container isolation
tests alone.

## Workload boundary under review

The preferred experiment is to contain the entire workload cluster behind an
outer user namespace, retaining normal cluster administration and node root
inside that environment. The trusted runner and verifier remain outside it on
the same physical server. Workload UID 0 must map to an unprivileged host UID
with no access to the runner's files, processes, credentials or Docker daemon.
Rootless Kind is the first candidate because Kind supports it explicitly.
This is a deployment experiment, not a completed protection in this change.

This better preserves the benchmark's operator action space than introducing a
restricted repair API. Even a fixed API can exclude legitimate novel repairs.
If containment cannot support a required host operation, a managed platform
interface needs a separate compatibility decision and benchmark version.
Permissions and tool availability must not depend on the injected fault,
since that can disclose its cause and change task difficulty. Broad namespace
or privilege bans have not been enabled by this change.

The catalog requires more than application-only administration:

| Existing operation | Task examples | Compatibility requirement |
| --- | --- | --- |
| Application exec, configuration, secrets and persistent data | Database, configuration and workload faults | Preserve ordinary repair operations within the application environment. |
| CoreDNS configuration and restart | Stale CoreDNS configuration | Permit the relevant system-service repair without exposing harness credentials. |
| Scoped RBAC, admission and networking changes | Operator, webhook, PSA and Calico faults | Validate escalation and controller effects; blanket write bans would remove legitimate repairs. |
| Kubelet configuration and service management | Kubelet crash and eviction threshold | Preserve node-local files, systemd and runtime control within the outer boundary; qualify actual repairs. |
| Clock maintenance | Native node clock drift / portable TLS validation-clock v2 | Real nodes retain the native task; emulated clusters use a versioned service-clock fault without host clock writes. |

The native clock task executes `nsenter ... date -s` on a privileged node. Its
oracle compares node time to runner time. On same-host Kind these read the
same system clock, so the skew check does not independently establish recovery.
Direct native-clock injection now rejects emulated or rootless workloads before
mutation. The registry resolves the existing clock ID to TLS validation-clock
v2 on emulated clusters, while real nodes retain native-node-clock v1. The new
`tls_clock_drift_hotel_reservation` ID explicitly selects the portable variant
on either platform. Trusted results record `task_version`; results from these
versions must not be compared as if their repair action spaces were identical.
The audit never changed the physical-host clock.

The rootless experiment must use SREGym's pinned node image and Calico 3.29.3,
including multiple nodes, local PVC recovery, network policy, kubelet recovery
and actual conntrack behavior. A default Kind smoke test is insufficient. The
current `/run/udev` host mount needs explicit review. Kind documents limits on
block devices and NFS, and containers cannot independently set realtime clocks.
If concrete compatibility failures justify it, an outer system container such
as Sysbox is a second research option, not an assumed drop-in replacement.

A disposable CloudLab experiment on Ubuntu 24.04 / Linux 6.8, Docker 29.1.3
and Kind 0.27.0 established the following limited feasibility evidence:

| Check | Observed result |
| --- | --- |
| SREGym's pinned Kubernetes 1.32.1 image, four nodes, Calico 3.29.3 | All nodes and Calico pods became Ready. |
| Cross-node HTTP, cluster DNS, local-path PVC write/read | Passed. |
| Node configuration access, kubelet configuration edit/restart, kubelet stop/start | Passed in the disposable environment. |
| Node-root identity | UID 0 mapped to unprivileged physical-host UID 20041. |
| Runner-owned private canary | Unreadable by the sandbox user and by a privileged rootless container even when deliberately mounted read-only. |
| Trusted Docker daemon | Its socket was unavailable to the sandbox user and absent from the node. |

The experiment omitted the `/run/udev` host mount, used a dedicated unprivileged
account and runtime directory, and did not disable host AppArmor restrictions.
Calico also emitted IPIP tunnel warnings with an `Always` IPIP pool and no
`tunl0`, so readiness and a successful basic traffic check are not complete
network-fault qualification. Rootless Docker also reported missing cpuset and
several I/O limit capabilities; resource-fault equivalence needs testing. The disposable
nodes and daemon were stopped after testing. These checks do not prove immunity
to container escape, establish conntrack fault behavior, or qualify the full
task catalog. They justify testing this deployment before narrowing repair
permissions.

The workload runtime must have a separate daemon identity and data directory
from the verifier runtime. The experimental integration below makes that
selection explicit; setting `DOCKER_HOST` for the whole runner would not
implement the separation. Kubernetes access from the outer verifier also needs
a reachable, TLS-verified endpoint without sharing the workload daemon socket.
Every process that executes agent-selected code must live inside the workload
boundary as well. Experimental rootless runs now reject host-mode agent
registrations and external harness lifecycles before injection. Those paths
need an execution audit; a trusted connector to an external agent is different
from executing the agent's shell as the runner user.

This boundary must cover direct Kubernetes access, MCP execution,
service-account credentials, RBAC escalation, privileged workloads, host mounts,
infrastructure controllers and runtime sockets. Existing proxy hiding is not
authorization against an operator who obtains another cluster credential.
In particular, MCP kubectl configuration and credential-plugin execution need
their own review; a protected namespace response test does not qualify them.

Node root can inspect probe pods running on its nodes and falsify internal
logs, metrics or API responses even if the outer verifier stays inaccessible.
Probe scripts therefore must not carry private answers or baseline data.
External behavioral checks and private pre-fault data invariants remain needed;
container isolation cannot make agent-controlled observations authoritative.

For each supported task, qualification must establish a healthy baseline, a
failed no-op after injection, and a passing reference repair performed through
the same operator interface available to the agent. Independent adversarial
checks must reject verifier reads/writes, process and runtime access, credential
theft, forged/replayed results, and attempts to disable grading evidence.
Passing serialization tests or a generic deployment recovery test does not
qualify the complete catalog. Changes to permissions or repair interfaces must
be versioned and recorded alongside evaluation results.

## Experimental rootless integration

Rootless execution is opt-in while catalog qualification is incomplete. The
ordinary deployment and existing repair interfaces remain the default. The
experimental path retains workload cluster administration and node root inside
the outer user namespace; it does not select permissions according to the fault.

The trusted runner and verifier use a different engine from the workload cluster
and evaluated agent. `SREGYM_TRUSTED_DOCKER_HOST` is captured for verifier image
builds, execution, cancellation and cleanup, and for CLI judge containers. Other
workload Docker calls retain `DOCKER_HOST`. Preflight rejects an overriding
`DOCKER_CONTEXT`, a shared engine, a workload socket owned by root or the runner,
and a workload engine lacking rootless mode or cgroup v2. Both CLI and embedded
conductor entrypoints validate the selected Kubernetes nodes and published API
port against that workload engine, preventing accidental use of a rootful Kind
cluster. The private workload kubeconfig must belong to the trusted runner and
use verified HTTPS on the configured runner address.

Use a dedicated Linux account with no sudo, trusted Docker group membership or
access to runner-owned private directories. Delegate the CPU, cpuset, I/O,
memory and PID controllers to its user service. Keep its runtime directory and
data directory separate, and grant only the trusted operator permission to use
its socket. Do not grant the workload account access to the trusted socket.
Use separate bridge subnets for the engines. In the Kind configuration, replace
the API loopback address with a reachable physical-host address that is also
reachable from the trusted verifier's bridge. Omit the `/run/udev` host mount.
Load required host networking modules before creating the cluster; do not
disable AppArmor or user-namespace restrictions globally.

For example, the existing CloudLab validation environment uses:

```bash
export SREGYM_ROOTLESS_WORKLOAD=1
export DOCKER_HOST=unix:///run/user/20041/docker.sock
export SREGYM_TRUSTED_DOCKER_HOST=unix:///var/run/docker.sock
unset DOCKER_CONTEXT
export SREGYM_RUNNER_ADDRESS=172.17.0.1
export KUBECONFIG=/private/rootless-workload.kubeconfig
export SREGYM_CLUSTER_BASELINE_FILE=/private/rootless-cluster-baseline.json
export SREGYM_ROOTLESS_TEST_KUBECONFIG="$KUBECONFIG"
uv run pytest -m integration tests/service/test_rootless_integration.py
```

Account IDs, addresses and private paths are deployment inputs, not universal
defaults. The dedicated baseline file prevents experimental reconciliation
from reusing the ordinary cluster's cached state. A benchmark run records
`workload_boundary=rootless-docker-v1-experimental` in its trusted results.

Agent credentials and selected application inputs are transferred into workload
volumes through Docker's copy API, without granting the workload daemon read
access to private runner paths. Agent output volumes start empty. Outputs are
collected only into the run's `agent/` directory after the container is reaped;
they cannot replace trusted result, phase or audit files at the run root. The
collector bounds archive size, file count and collection time and rejects links,
devices, traversal, duplicate paths and file/directory conflicts. Existing flat
trajectories still convert, while nested agent trajectories obtain result
metadata from the trusted run root. Filtered egress stages only its two public
Python modules in a traversable temporary directory; credentials are never
staged there.

The follow-up CloudLab run used explicit cgroup delegation and loaded `ipip`.
Docker then reported no missing resource-controller warnings and Calico created
`tunl0`. Real checks established separate engines, volume-only agent mounts,
enforced agent CPU/memory limits, metadata protection, and healthy/no-op/reference
repair grading through the outer verifier. These checks do not qualify every
fault. Linux 6.8 makes `nf_conntrack_max` a global, read-only setting outside the
initial network namespace. Conntrack v2 calibrates traffic against that existing
limit and never writes it. Linux still does not namespace the realtime clock;
the portable clock variant therefore explicitly changes the simulated fault to
the TLS client's validation clock.
The operational qualification also passed cross-node IPIP traffic, IPv4 DNS,
network-policy injection/removal, pod CPU/memory limits and PVC data preservation
across pod recreation. Initial OOM checks failed status fidelity: the kernel
killed a 64 MiB pod allocating 128 MiB, but Kubernetes reported `Error`.
Investigation found the shared rootless UID had exhausted all 128 permitted
inotify instances; creating another instance returned `EMFILE`, and Jaeger
could not create its filesystem watcher. Host provisioning increased
`fs.inotify.max_user_instances` to 512, matching
[Kind's documented prerequisite](https://kind.sigs.k8s.io/docs/user/rootless/#increase-inotify-limits).
The experimental preflight now requires that minimum and records the configured
budget. It only reads host configuration; provisioning changes are never made
by the benchmark runner or exposed as an agent tool. Larger catalogs may need
more headroom, and this minimum does not reserve instances against workload DoS.

After provisioning, all eight operational checks passed, including genuine
`OOMKilled` reporting and denial of host-global conntrack-limit writes. The
rootless integration suite passed four checks. Its OOM control creates nine
genuine OOMs and distinguishes them from both an explicit exit 137 and a real
SIGKILL, which correctly retain `Error`. The SIGKILL is sent from the logical
node's ancestor PID namespace because namespace init ignores such a signal
sent from inside its own namespace. These controls preserve existing grading
behavior; they never manufacture a reason or accept exit 137 as sufficient OOM
evidence. Neither the runtime version nor the graders changed, and
`kernel.dmesg_restrict=1` was retained. The affected tasks below were then
qualified with their existing graders; the complete catalog gate still remains
necessary before changing the default.

Conntrack v2 reads the existing limit, generates 105% of that many socket
attempts, and retains the original 98% injection and 10% recovery thresholds.
It never lowers or restores a host-global limit. Larger tables receive enough
destination ports for the usual ephemeral-port range, and the gateway address
is resolved once before spawning traffic workers. A 2097152-connection generator
budget rejects an unaffordable table before creating traffic; this is an
infrastructure qualification failure, not an agent score. Unit cases cover
calibration through a 1048576-entry table; actual saturation on larger hosts
still needs qualification. The existing grader and repair interface are retained. The v2 full application
qualification passed healthy / injected no-op / reference-repair checks with the
original 19-Deployment baseline and unchanged host limit.
On this server the table is 262144. The reference operator stops the traffic
source and flushes the logical node's table using only `NET_ADMIN` and
`SYS_CHROOT`, without a privileged flag or physical-host root access.

TLS validation-clock v2 creates a healthy TLS dependency and client before
capturing the private certificate/trust and 20-Deployment baseline. The client's
OpenSSL handshake uses its configured validation-time offset. A 30-day offset
makes the currently valid dependency certificate appear expired, causes actual
TLS handshakes to fail, and removes Ready frontend endpoints. Restoring the
client configuration repairs the fault without any node clock change or new
runtime image. This models a service's TLS verification clock, not a virtualized
Linux `CLOCK_REALTIME` or NTP daemon.

Its grader requires the captured application baseline, unchanged TLS identity
and trust, the actual validation/readiness contract in both the desired template
and live frontend pods, a clock offset within 60 seconds, and a fresh real TLS
handshake. Kubernetes' automatically injected service-account token mount is
recognized separately from the application mounts. No private baseline, verdict
or private key is placed in a workload probe. Full application qualification
passed healthy, injected no-op and operator repair, and rejected readiness
bypass, TLS-client removal and frontend scaling to zero. Recovery API errors
propagate instead of being suppressed by the shared optional recovery wrapper.
These checks reject those concrete bypasses; they do not make observations from
an agent-controlled node authoritative or establish zero reward hacking.

Both affected OOM tasks also completed healthy / injected no-op / reference-repair
checks through the outer verifier and the normal agent container. The Social
Network mutating-webhook task produced a real `OOMKilled` termination and passed
after the operator removed the webhook and restarted the deployment. The
nightly Hotel Reservation task's 4 MiB limit can instead kill container init;
its `StartError` included the runtime's explicit OOM message and the node's
kernel OOM counter increased. A separate 4 MiB comparison reproduced that
bootstrap outcome on rootful Kind as well. The existing injector and oracle
already support this outcome; their limits and acceptance criteria were not
changed. A temporary repair failed while the recurring CronJob remained active,
an actor Job actually succeeded, and suspending the actor plus restoring memory
passed. All four application qualifications completed fault and application
cleanup, and the four rootless nodes remained Ready.

The task reworks resolve the host-operation compatibility issues through
explicit versioning and workload-local operations. They preserve ordinary native
node behavior outside emulated clusters. The persistent external-harness lifecycle
and full catalog/adversarial qualification remain outstanding. Larger conntrack
tables within the traffic budget have unit calibration coverage, not completed
physical saturation qualification. Rootless remains opt-in during that work.

Neither host conntrack limit nor wall clock was changed during qualification. See the
[kernel implementation](https://github.com/torvalds/linux/blob/v6.8/net/netfilter/nf_conntrack_standalone.c)
and [Docker cgroup delegation guidance](https://docs.docker.com/engine/security/rootless/tips/#limiting-resources).

## Validation

The latest complete regression run passed 2291 tests and seven subtests, with
three skips and 28 failures. Comparing failure identifiers against an unchanged
upstream checkout found the same 28 failures and no new ones. Real integration
runs passed 11 verifier checks on rootful Kind and four on the experimental
rootless deployment, plus the full TLS-clock application qualification. The
Gitless packaged-runtime check also passed manifest
validation, untracked-file exclusion, exact Python compatibility, image identity
and real worker execution. These numbers describe regression and integration
coverage, not full task catalog or LLM evaluation results.

Run the unit and lifecycle checks with
`uv run pytest tests/service/test_verifier_runtime.py`. To exercise real Docker
execution, create a disposable Kind cluster on the existing Docker host,
then run:

```bash
SREGYM_VERIFIER_TEST_KUBECONFIG=/private/test-cluster.kubeconfig \
  uv run pytest -m integration tests/service/test_verifier_integration.py
```

Run the task-specific portable clock qualification with the same explicit
rootless environment using
`uv run pytest -m integration tests/problems/test_task_rework_integration.py`.
It deploys Hotel Reservation, refuses to overwrite an existing application
namespace, exercises operator repair and the three concrete grading bypasses,
and cleans up the application afterward.

These opt-in tests create and remove their temporary application namespace and
may create the reusable, protected node-probe namespace. They check process
hardening, private logs, forged subprocess output,
timeout/crash cleanup, live workload IO, loss of telemetry, Kubernetes node
configuration probes, and native conductor baseline/no-op/deletion/reference
recovery behavior against the same host oracle. They do not deploy the complete
problem catalog or substitute for task-specific recovery and data validation.

Docker hardening and bridge behavior follow the [Docker run reference](https://docs.docker.com/reference/cli/docker/container/run/)
and [bridge networking documentation](https://docs.docker.com/engine/network/drivers/bridge/).
Kind's privileged node configuration is defined in its [Docker provider](https://github.com/kubernetes-sigs/kind/blob/main/pkg/cluster/internal/providers/docker/provision.go).
The shared-clock limitation follows the [Linux time namespace documentation](https://man7.org/linux/man-pages/man7/time_namespaces.7.html).
The alternative deployment research follows [Kind rootless support](https://kind.sigs.k8s.io/docs/user/rootless/),
[Docker rootless isolation](https://docs.docker.com/engine/security/rootless/), and
[Sysbox's documented limitations](https://github.com/nestybox/sysbox/blob/master/docs/user-guide/limitations.md).

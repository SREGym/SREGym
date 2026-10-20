# mini-k8s: a lightweight Kubernetes for SREGym

Brainstorm and first experiments: how much of SREGym's compute is the
application under test, how much is the cluster around it, and how far a
simulated or half-simulated Kubernetes can cut the second part without breaking
problems and oracles.

**Status:** research prototype. Everything here was measured on one 4 vCPU /
16 GB cloud VM (cgroup v1, kernel 6.18). The prototype runs SREGym's own app
deploy code, workload generator, fault injectors and mitigation oracles
unchanged against both clusters.

## TL;DR

1. **On kind, the app is a small part of the bill.** With hotel-reservation
   deployed through SREGym's usual path (kind with 4 nodes and Calico, plus the
   per-problem infra stack), the app was **13% of memory and 2% of CPU at
   idle**. Calico, four kubelets, containerd and shims, kube-proxies, Prometheus,
   Loki, Promtail and OpenEBS were the rest: 4.6 GiB and 3.4 of the 4 cores
   before the agent does anything.
2. **Swapping containers for processes saves little on its own.** The app took
   712 MiB as containers and 637 MiB as processes. The savings come from
   removing per-node machinery (kubelet, containerd, shims, CNI and kube-proxy
   on every node) and from the per-problem infra stack.
3. **A prototype "mini cluster" works.** It runs a real control plane
   (KWOK's binary runtime: etcd, kube-apiserver, controller-manager,
   scheduler, as plain processes), a real kube-proxy and CoreDNS, and
   **procnode**, a ~1.9k-line Python node agent that runs every pod as Linux
   processes (netns per pod, overlay rootfs, cgroups, kubelet lifecycle
   semantics). KWOK keeps the Node objects and serves `logs`, `exec` and
   `port-forward`. That needed three small KWOK patches, one of them an
   upstream bug fix.
4. **Results against kind on the same host** (hotel-reservation, SREGym code
   unchanged):

   | | kind (SREGym setup) | mini cluster | |
   |---|---|---|---|
   | Cluster-platform memory, app idle | 2.28 GiB | **0.77 GiB** | 3.0× less |
   | Cluster-platform CPU, app idle | 0.42 cores | **0.07 cores** | 5.7× less |
   | Cluster setup, node image cached | 117 s (kind + Calico) | **46–76 s** (control plane alone: 4–6 s) | |
   | App deploy, images cached | 31.4 s | **5.6–8.8 s** | ~4× faster |
   | wrk2 mean latency, 100 rps | 68–79 ms (box saturated by infra) | **4.3 ms** | |
   | Fidelity: inject → oracle fails → recover → oracle passes | 12/12 | **12/12** | |

5. **Fidelity is the real constraint, not cost.** Of SREGym's 217 registered
   problems only about 22 (10%) show their fault purely in API state, and 15
   of the 17 SREGym-Lite oracles need real traffic, probe pods or
   `kubectl exec`. A pure simulator (KWOK with fake pods) is nearly free
   (7–16 MiB of control plane per simulated hotel-reservation environment,
   100 of them in 1.2 GiB) but can grade only that 10%. The prototype keeps
   real processes and a real data plane and covers every bucket except node-,
   kernel- and CNI-internal faults (see [coverage](#6-fault-coverage-and-known-gaps)).
6. **Recommendation:** treat "mini-k8s" as fidelity tiers behind one
   interface. Run most problems on process nodes (tier 1), run API-only
   faults on pure KWOK (tier 0), and keep kind/kubeadm for node and kernel
   faults (tier 2). For RL rollouts, the next big lever after footprint is
   reset time: snapshot and fork a warm environment, and serve diagnosis
   rollouts from a frozen snapshot. See the [roadmap](#7-roadmap).

---

## 1. Where SREGym's compute goes today

`bench/footprint.py` walks `/proc`, attributes every process to a component
(by command line and, for pods, by the pod UID in its cgroup path) and samples
CPU over 30 s. "PSS" is proportional set size, which splits shared pages
fairly. Raw data: `results/0*.json` (kind) and `results/1*.json` (mini).

**kind, 4 nodes + Calico (SREGym's `kind/setup_kind_cluster.sh`), hotel-reservation idle**

| Component | PSS | CPU (cores) |
|---|---:|---:|
| hotel-reservation (19 pods: 8 Go services, 6 MongoDB, 3 memcached, consul, jaeger) | 712 MiB | 0.13 |
| Calico (calico-node ×4, kube-controllers) | 755 MiB | 0.10 |
| Control plane (apiserver, etcd, controller-manager, scheduler) | 620 MiB | 0.13 |
| containerd + shims + pause (4 nodes) | 388 MiB | 0.08 |
| kube-proxy ×4 | 227 MiB | 0.00 |
| kubelet ×4 | 199 MiB | 0.10 |
| CoreDNS, local-path provisioner | 104 MiB | 0.01 |
| systemd and friends inside the node containers; host dockerd | 41 + 97 MiB | 0.00 |
| **Total** | **3,146 MiB** | **0.55** |

**Adding the infra stack `Conductor.deploy_app()` installs for every problem**
(metrics-server, OpenEBS, Prometheus with node-exporter ×4 and
kube-state-metrics, Jaeger, the OTel collector, Loki with Promtail ×4):

| | PSS | CPU (cores) |
|---|---:|---:|
| `observe` namespace (Prometheus, Loki, Promtail ×4, OTel, …) | 1,555 MiB | 0.81 |
| `openebs` (node-disk-manager ×4 and friends) | 282 MiB | 1.03 |
| containerd/shims (now 137 processes) | 545 MiB | 1.08 |
| hotel-reservation | 710 MiB | 0.07 |
| **Total, idle app** | **5,394 MiB** | **3.50 of 4** |
| **Total, wrk2 at 100 rps** | **5,452 MiB** | **3.57 of 4** (app 0.64) |

On this 4 vCPU box the infra alone nearly saturates the CPU, which is why wrk2
latency was 68–79 ms on kind against 4.3 ms on the mini cluster. That
comparison is not apples to apples (the mini cluster had no observability
stack), but it is the honest experience of running SREGym on a small box. The
repo's guidance of 8 vCPU / 16 GB per concurrent SREGym-Lite run matches these
numbers.

Setup and reset cost on kind (images cached unless noted):

| Step | Time |
|---|---:|
| hotel-reservation deploy (cold, with image pulls) | 86 s |
| hotel-reservation cleanup + redeploy (warm) | 31 s + 31 s |
| infra stack: metrics-server / OpenEBS / Prometheus / OTel | 33 / 33 / 103 / 17 s |
| Loki | 515 s (inflated here by an IPv6-only nginx bug in this sandbox) |

Two smaller findings from these runs:

- `memcached` is unpinned (`:latest`), so every SREGym deploy re-pulls it, and
  Docker Hub returned **429 Too Many Requests** during these experiments. The
  DinD README already warns about this. A shared image store fixes it, and
  the process-based design gets one for free (one unpacked rootfs per image
  per host).
- On kind, 5 of the 8 Go services restarted once while MongoDB was still
  starting. On the mini cluster everything came up faster and none
  restarted. Keep this in mind when comparing behaviour that depends on
  start-up races.

## 2. What a lighter substrate must preserve

Two codebase surveys (summarised here) drove the design.

**What the agent and the harness touch**

- The agent gets cluster-admin `kubectl` through `sregym/service/k8s_proxy.py`,
  with every verb including watches, `pods/exec`, `pods/attach` and
  `pods/portforward`. Stratus also gets MCP tools for kubectl, Prometheus,
  Jaeger and Loki.
- Oracles read API state, but 15 of the 17 SREGym-Lite mitigation oracles
  also send real traffic. They start busybox or grpcurl probe pods that run
  `nc`, `wget` or `nslookup` against Service DNS names and read the pod's
  logs, `kubectl exec` into Valkey, Postgres or Mongo, read wrk2 output, or
  query Prometheus alerts.
- Workloads (wrk2, Locust) run in-cluster and are read back with `pods/log`
  plus `exec date`.
- Some injectors touch nodes directly: `docker exec` into kind nodes (kubelet
  crash, eviction thresholds), privileged `nsenter` pods (clock drift,
  conntrack) and hostPath pods (WiredTiger corruption).

**Fault taxonomy, all 217 registered problems** (117 direct, 84 Incident Arena
ports, 16 SLO artifacts):

| Bucket | What the fault needs | Problems | Pure KWOK (fake pods) | Mini cluster (this prototype) |
|---|---|---:|---|---|
| A1 | API mutation; symptom in API state (Pending, admission reject, no endpoints) | 22 | ✅ | ✅ validated |
| A1k | API mutation; symptom produced by kubelet (ImagePullBackOff, FailedMount, probes, init containers) | 21 | partly, with custom Stages | ✅ validated |
| A2 | API mutation; the real app must misbehave (bad env, port, selector) | 24 | ❌ | ✅ validated |
| B | App runtime (DB auth, flags, Kafka, load-driven metastability) | 83 | ❌ | ✅ validated (Mongo auth); alert-graded ones need the obs stack |
| C | Network data plane (NetworkPolicy, DNS, kube-proxy, Calico, ingress) | 26 | ❌ | ✅ NetworkPolicy (validated); DNS faults expected (CoreDNS is real); ❌ Calico CRDs, patched kube-proxy |
| D | OS, kernel or node (OOM, CFS throttling, sysctls, kubelet, conntrack, clock) | 21 | ❌ | partly, expected: real cgroups (OOM, throttling) and namespaced sysctls; ❌ kubelet and node internals |
| E | Control plane (webhooks, PSA, preemption, operators, finalizers) | 20 | partly | ✅ expected (real apiserver, scheduler, controllers) |

So a pure simulator gives up roughly 90% of the benchmark. The design has to
keep real processes and a real data plane, and remove everything else.

## 3. Design space

| Option | Idea | Saves | Keeps | Loses |
|---|---|---|---|---|
| **Pure KWOK** (tier 0) | Real apiserver; pods are status only (KWOK Stages) | Nearly all compute: 7–16 MiB per environment | A1, some A1k and E | Anything that needs traffic, exec or logs |
| **k3s / k0s single node** | A lighter real Kubernetes | Some control-plane memory | Everything | Still containerd, kubelet, CNI; single node only |
| **Shared cluster, namespace per environment** (or vcluster) | Amortise infra over many environments | Infra per environment | App-level faults | Isolation for cluster-scoped faults (CoreDNS, nodes, webhooks, CRDs, PriorityClasses) unless virtualised |
| **Process nodes** (tier 1, this prototype) | Real control plane and data plane; pods are processes; no kubelet, containerd or CNI | Per-node daemons, shims, image duplication | A–C, most of D and E | Node and kernel internals; a single kernel shared by all "nodes" |
| **Full kind / kubeadm** (tier 2) | Today's setup | — | Everything | — |
| **Snapshot / replay** | Fork a warm environment; serve read-only diagnosis rollouts from a snapshot | Setup and reset time; whole environments for diagnosis | Depends on the tier underneath | Mitigation still needs a live environment |

**Where the three inspirations fit**

- **KWOK** is a good control-plane and "node facade": real API machinery,
  node registration and leases, and a kubelet-compatible server for
  `logs`, `exec`, `port-forward` and metrics, driven by small CRDs. On its own
  it is tier 0. With a process agent behind it, it is tier 1.
- **supervisord**: running processes instead of containers is the right
  instinct, but pods need kubelet semantics that supervisord does not model:
  CRI-format timestamped logs (KWOK and SREGym's wrk2 reader depend on them),
  CrashLoopBackOff with exponential backoff and `restartCount`, exit codes and
  `OOMKilled`, probes, init-container and native-sidecar ordering, and
  graceful termination. procnode does that supervision in a few hundred
  lines of asyncio. supervisord is still a good fit one level up, supervising
  each environment's daemons (etcd, apiserver, controller-manager, scheduler,
  kwok-controller, kube-proxy, procnode), where today kwokctl and nohup do it.
- **Kamera** model-checks controller-runtime reconcilers against an in-memory
  state model and explores reconcile interleavings. It is not a substrate for
  an agent that needs a real API and real traffic, but two of its ideas
  carry over. (a) As a **problem generator**: exploring operator and
  controller interleavings can surface realistic control-plane failure states
  for new E-bucket problems (SREGym already has six TiDB-operator and several
  finalizer and webhook problems). (b) Its **trace capture and replay**
  maps onto the snapshot/replay lever above, because the diagnosis stage is
  graded by an LLM judge on text and is mostly read-only.

## 4. The prototype

```
            one host (4 vCPU / 16 GB VM)
 ┌───────────────────────────────────────────────────────────────────────┐
 │ control plane (kwokctl --runtime binary, plain processes)             │
 │   etcd · kube-apiserver · controller-manager · scheduler              │
 │   kwok-controller (patched) ── Node objects + leases for              │
 │        kind-control-plane, kind-worker{,2,3}                          │
 │        kubelet API: logs / exec / port-forward  ◄── apiserver         │
 │                                                                       │
 │ kube-proxy (iptables, host netns)   ── Service VIPs for every pod     │
 │ procnode (python, ~1.9k lines)      ── owns all pods on those nodes   │
 │   per pod:   netns + veth (/32, routed via host) · uts/ipc ns         │
 │              /etc/hosts, resolv.conf, shm · NetworkPolicy (iptables)  │
 │   per ctr:   overlay(image rootfs) · mount+pid ns · cgroup mem/cpu    │
 │              CRI logs in /var/log/pods · probes · restarts · events   │
 │   storage:   hostPath provisioner for PVCs                            │
 │ CoreDNS: an ordinary Deployment that procnode runs like any other pod │
 └───────────────────────────────────────────────────────────────────────┘
```

**What procnode implements** (`procnode/procnode.py`, `procnode/netpol.py`)

- **Pod sandbox:** a network namespace with a /32 address routed through
  the host (Calico-style, so the host's iptables, and therefore the real
  kube-proxy, see every packet), plus shared UTS (hostname = pod name) and
  IPC namespaces. Containers in a pod share these, so
  `sidecar_port_conflict` behaves as it does on Kubernetes.
- **Containers:** each image is unpacked once with docker (used only as an
  image store), and each container instance gets its own overlay upper
  layer. Containers get a private mount and PID namespace (the app is PID 1)
  and per-container cgroup v1 memory and CPU limits, so `OOMKilled` and CFS
  throttling are real. Mount targets are resolved like runc's securejoin, so
  image symlinks cannot escape the rootfs (an early version followed an
  absolute `/var/run` symlink out of the container).
- **Kubernetes semantics:** env including `valueFrom`, `envFrom` and service
  links; command/args against the image's ENTRYPOINT/CMD; volumes (emptyDir
  including memory medium, configMap, secret, projected ServiceAccount
  tokens through TokenRequest, downwardAPI, PVC, hostPath, subPath, fsGroup);
  `dnsPolicy`, `dnsConfig` and `hostAliases`; namespaced sysctls; startup,
  liveness and readiness probes (HTTP, TCP, exec); init containers and native
  sidecars; `restartPolicy`; CrashLoopBackOff; graceful termination; kubelet
  events; and the `ErrImagePull`, `ImagePullBackOff`, `FailedMount` and
  `CreateContainerConfigError` waiting reasons.
- **Streaming:** logs are written in CRI format to kubelet's layout
  (`/var/log/pods/<ns>_<pod>_<uid>/<container>/<n>.log`), so `kubectl logs`,
  `--previous` and `--timestamps` work through KWOK. `kubectl exec` runs
  through a helper that `nsenter`s the container's namespaces and root.
- **NetworkPolicy:** pod/namespace selectors, ipBlock and named ports, compiled
  to per-pod iptables chains. Probe traffic is fwmarked so it bypasses
  policy, as on a real node.
- **Restart:** if procnode is killed it reaps sandboxes on start and re-runs
  pods with the same IP (the API rejects podIP changes), like a node reboot.

**KWOK patches** (`cluster/kwok-procnode.patch`, 17 lines, against KWOK main):

1. `KWOK_EXEC_PREFIX`: KWOK's exec runs the command on the host. The prefix
   lets the node agent wrap it in `nsenter`.
2. A `PortForward` target port of 0 means "the requested port", so one
   object per pod covers every port.
3. **Upstream bug:** KWOK never closed the stdin pipe of an exec, so
   `kubectl exec -i pod -- sh < script` hung forever. SREGym's Mongo auth
   injectors do exactly this.

**Bring-up** is `cluster/up.sh`: control plane, nodes and StorageClass, host
forwarding, kube-proxy, procnode, CoreDNS. Teardown is `cluster/down.sh`.

## 5. Results

### 5.1 Footprint (hotel-reservation, same SREGym deploy code)

PSS in MiB / CPU in cores:

| | kind, app idle | mini, app idle | mini, wrk2 100 rps |
|---|---:|---:|---:|
| App | 712 / 0.13 | 637 / 0.12 | 847 / 0.91 |
| Control plane | 620 / 0.13 | 543 / 0.07 | 535 / 0.06 |
| Node agents (kubelet ×4 / procnode + kwok-controller) | 199 / 0.10 | 134 / 0.00 | 134 / 0.00 |
| Container runtime (containerd + shims / none) | 388 / 0.08 | — | — |
| CNI + kube-proxy (Calico + 4 kube-proxies / 1 kube-proxy) | 982 / 0.11 | 47 / 0.00 | 46 / 0.00 |
| DNS + addons | 104 / 0.01 | 66 / 0.01 | 67 / 0.01 |
| systemd inside kind node containers | 41 / 0.00 | — | — |
| Host docker (kind's runtime / mini's image store) | 97 / 0.00 | 155 / 0.00 | 149 / 0.00 |
| **Total** | **3,146 / 0.55** | **1,616 / 0.19** | **1,812 / 0.98** |

Leaving out the app and the host's docker daemon, the platform costs
**2,334 MiB and 0.42 cores on kind** against **790 MiB and 0.07 cores on the
mini cluster**. In the mini cluster docker only serves as an image store and
could be replaced by a pre-unpacked image cache.

### 5.2 Time

| | kind | mini |
|---|---:|---:|
| Cluster setup (`kind/setup_kind_cluster.sh` / `cluster/up.sh`) | 117 s (node image cached; Calico images pulled) | 46–76 s, mostly waiting for CoreDNS to report Ready |
| Control plane alone | — | 4–6 s |
| App deploy, first time | 86 s (includes registry pulls) | 27.9 s (images already in the local store; includes unpacking them) |
| App deploy, warm | 31.4 s | 5.6–8.8 s |
| App cleanup | 31.3 s | 23.1 s (mostly SREGym's fixed sleeps and namespace polling) |

### 5.3 Fidelity

`bench/fidelity.py` drives each problem through SREGym's own code: capture
the oracle baseline, `inject_fault()`, wait 30 s, evaluate the mitigation
oracle (a faithful environment makes it fail), `recover_fault()`, wait 30 s,
evaluate again (it should pass).

| Problem | Bucket | kind | mini |
|---|---|:-:|:-:|
| wrong_service_selector_hotel_reservation | A1 | ✅ | ✅ |
| namespace_memory_limit (Lite) | A1 | ✅ | ✅ |
| missing_configmap_hotel_reservation | A1k | ✅ | ✅ |
| readiness_probe_misconfiguration_hotel_reservation | A1k | ✅ | ✅ |
| init_container_dependency_hang_hotel_reservation | A1k | ✅ | ✅ |
| service_wrong_pod_selection_hotel_reservation (Lite) | A2 | ✅ | ✅ |
| missing_service_hotel_reservation | A2 | ✅ | ✅ |
| wrong_bin_usage | A2 | ✅ | ✅ |
| sidecar_port_conflict_hotel_reservation | A2 | ✅ | ✅ |
| configmap_drift_hotel_reservation | A2 | ✅ | ✅ |
| network_policy_block (Lite) | C | ✅ | ✅ after adding NetworkPolicy enforcement (❌ before) |
| revoke_auth_mongodb-1 | B | ✅ | ✅ after the KWOK stdin fix (hung before) |

Both mini-cluster failures were found by this harness and fixed. Raw data:
`results/fidelity-*.json`.

### 5.4 Scaling

**Tier 0: pure KWOK, fake pods** (`bench/kwok_fake_scale.sh`; control-plane
RSS in MiB for N hotel-reservation environments, 19 pods each, on one control
plane with 25 fake nodes):

| Environments | Pods | etcd | apiserver | controller-manager | scheduler | kwok | Total | Per environment |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 | 0 | 47 | 252 | 94 | 49 | 55 | 497 | — |
| 1 | 19 | 52 | 267 | 105 | 53 | 57 | 534 | 37 |
| 10 | 190 | 80 | 324 | 129 | 63 | 58 | 654 | 16 |
| 50 | 950 | 128 | 466 | 182 | 101 | 61 | 938 | 9 |
| 100 | 1,900 | 166 | 565 | 242 | 133 | 62 | 1,168 | 7 |

The cluster came up in 3.2 s; going from 50 to 100 environments took 3 minutes.
A simulated environment costs megabytes, not gigabytes, but it can only grade
the API-state problems. Even tier 0 needs storage semantics: without a
provisioner, the MongoDB pods' PVCs never bind and the pods stay Pending, so
the script pre-binds a PV to each claim. Also note that KWOK's fake nodes
default to 110 pods each.

**One isolated control plane per environment** (`bench/cp_cost.sh`, idle):

RSS in MiB, mean of three clusters each (`results/cp-cost.csv`):

| | etcd | apiserver | controller-manager | scheduler | kwok | total | create |
|---|---:|---:|---:|---:|---:|---:|---:|
| default | 45 | 260 | 92 | 48 | 49 | **495** | 4–6 s |
| `GOGC=50 GOMEMLIMIT=150MiB` | 42 | 205 | 90 | 48 | 51 | **435** | 4–4.5 s |

**Tier 1 density: N real hotel-reservation environments on one mini cluster**
(`bench/density.sh`; each copy in its own namespace, deployed with SREGym's
code, on top of the base cluster plus the original `hotel-reservation`):

| Extra environments | Pods | Host memory used | Idle CPU (cores) |
|---:|---:|---:|---:|
| 0 | 21 | 1,487 MiB | 0.23 |
| 1 | 40 | 1,989 MiB | 0.25 |
| 4 | 97 | 3,401 MiB | 0.39 |
| 6 | 135 | 4,087 MiB | (still deploying) |

Each additional real environment cost about **450–500 MiB of RAM and 0.04
idle cores**. Two limits showed up before RAM or CPU did:

- **Scheduling.** The fake nodes advertise this host's 4 CPUs, as kind's nodes
  do, so the scheduler stopped placing pods after about 6 copies because the
  CPU requests no longer fit (`Insufficient cpu`). kind on this box would hit
  the same wall. Advertising more CPU (deliberate overcommit for mostly idle RL
  environments) let the run continue.
- **Disk.** Every MongoDB preallocates about 300 MB of WiredTiger journal, so
  each hotel-reservation copy writes **~1.8 GB** to its PVs. At about 10
  copies the VM's disk allowance (~40 GB) ran out and etcd died. This costs
  the same on kind. For dense RL runs it is worth giving MongoDB smaller or
  disabled journals, or tmpfs-backed PVs.

For comparison, the kind measurements above put a single SREGym environment
(with the infra stack) at 3.5 of this host's 4 cores while idle, so one is the
practical maximum on this box.

## 6. Fault coverage and known gaps

Validated on the mini cluster: everything in 5.3. Expected to work but not yet
run: the other A1, A1k and A2 problems, CoreDNS and DNS-policy faults (CoreDNS
is real), PSA, quota and preemption (the real apiserver and scheduler),
admission webhooks (the apiserver reaches Services through kube-proxy's OUTPUT
rules), OOMKilled and CFS throttling (real cgroups), ephemeral-port and other
namespaced sysctls, and FD limits.

Known gaps and how to close them:

| Gap | Affects | Fix |
|---|---|---|
| Observability stack not yet run on the mini cluster | AlertOracle (~22 problems), MCP tools | Try SREGym's charts as-is: Promtail already finds logs in kubelet's layout. Or run one shared Prometheus/Loki/Jaeger per host instead of per problem |
| `metrics.k8s.io` and kubelet `/stats/summary` | `kubectl top`, imbalance and HPA problems | procnode knows real per-container cgroup usage; publish it through KWOK's ResourceUsage CEL or serve `/metrics/resource` itself |
| One kernel, one kube-proxy for all "nodes" | `internalTrafficPolicy: Local`, hostNetwork DaemonSets (simulated on non-primary nodes), node failure | A network namespace per "node" with its own kube-proxy |
| No kubelet, containerd or Calico | kubelet crash and eviction config, the patched kube-proxy image, Calico IPPool/BGP, chaos-daemon NetworkChaos | Emulate the cheap ones in procnode (eviction thresholds, a tc-based NetworkChaos inside pod netns); route the rest to tier 2 |
| Node-level injectors use `docker exec` into kind nodes | kubelet_crash, conntrack, disk pressure | A provider-specific injector path, through the existing `KubeCtl.is_emulated_cluster()` hook |
| KWOK CRDs (`logs/execs/portforwards.kwok.x-k8s.io`) are visible | Agent may discover the simulator | Filter the `kwok.x-k8s.io` group in `k8s_proxy.py`, as is already done for `chaos-mesh.org` |
| Security | procnode runs pods as host root without seccomp, AppArmor or user namespaces | Fine for a sandboxed VM per run; add user namespaces before sharing hosts |
| ConfigMap volume updates, preStop hooks, ephemeral containers, `kubectl debug`, attach | Rare in SREGym | Small additions |

## 7. Roadmap

1. **SREGym-Lite end to end on the mini cluster.** Add the mini cluster as a
   cluster provider in the conductor (bring-up, `is_emulated_cluster()`
   routing, a node-level injector path), run the observability stack (or a
   shared one), filter KWOK CRDs from the agent, then run the full Lite
   suites with a real agent and compare scores and per-problem oracle traces
   with kind.
2. **Compatibility matrix for all 217 problems.** Extend `bench/fidelity.py`
   to every problem with recover support, on kind and on mini, and store the
   matrix in the repo so the registry can route each problem to a tier.
3. **Density.** Give each environment its own network namespace so N
   isolated clusters (each ~0.5 GB of control plane plus the app) share a
   host; supervise each environment's daemons with supervisord or systemd;
   share one image store across environments.
4. **Fast reset for RL.** The control plane and app restart in seconds
   already. Next is fork from a warm snapshot (etcd snapshot, PV and overlay
   copy-on-write, optionally CRIU for process state, which is far easier
   with plain processes than with nested containers), plus a
   read-only replay mode so many diagnosis rollouts share one live
   environment.
5. **Tier 0 at scale.** Use pure-KWOK environments for the API-state problems
   (A1, part of E) at hundreds of environments per host. Consider
   Kamera-style controller exploration to generate new control-plane
   problems.

## 8. Reproducing

All commands run from the SREGym checkout, as root (footprint.py reads
every process's smaps; procnode manages namespaces, mounts and iptables).

```bash
# kind baseline (SREGym's own setup script)
bash kind/setup_kind_cluster.sh
uv run python experiments/mini-k8s/bench/hr_deploy.py deploy
uv run python experiments/mini-k8s/bench/obs_deploy.py           # optional infra stack
python3 experiments/mini-k8s/bench/footprint.py --interval 30 --pods-json <(kubectl get pods -A -o json)

# patched kwok (needs Go)
git clone https://github.com/kubernetes-sigs/kwok /tmp/kwok
git -C /tmp/kwok apply "$PWD/experiments/mini-k8s/cluster/kwok-procnode.patch"
(cd /tmp/kwok && go build -o kwok ./cmd/kwok)

# mini cluster (needs docker as an image store, iproute2, iptables, conntrack, kwokctl)
KWOK_BIN=/tmp/kwok/kwok PYTHON=$PWD/.venv/bin/python experiments/mini-k8s/cluster/up.sh mini
export KUBECONFIG=~/.kwok/clusters/mini/kubeconfig.yaml
uv run python experiments/mini-k8s/bench/hr_deploy.py deploy
uv run python experiments/mini-k8s/bench/fidelity.py out.jsonl network_policy_block wrong_bin_usage
experiments/mini-k8s/cluster/down.sh mini
```

### Sandbox notes

The test VM differed from a typical SREGym host in ways that needed
workarounds. None of them is SREGym's fault:

- The VM forbids lowering `oom_score_adj`, so runc inside kind nodes failed
  for every pod sandbox ("can't get final child's PID from pipe"). Fixed by
  wrapping runc in the node image to clamp `oomScoreAdj` to 0.
- The egress proxy listened only on the host's loopback, so kind nodes
  reached it through `bench/tcpfwd.py`.
- With no IPv6, Loki's gateway nginx (`listen [::]:8080`) crash-looped and
  needed a config patch.
- Recent kind node images refuse cgroup v1, so SREGym's pinned v1.32 image
  was used.
- The session's disk allowance was about 40 GB, which ended the density run
  (see 5.4).

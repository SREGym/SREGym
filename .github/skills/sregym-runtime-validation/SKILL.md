---
name: sregym-runtime-validation
description: >-
  How to runtime-validate SREGym auto-generated database-bug problems (auto_<db>_<n>.py) —
  actually deploy each on a kind cluster, inject the fault, and observe whether the bug MANIFESTS
  on the cluster (not grade an agent). Covers the demo-agent no-API-key validation loop, what the
  observable signal is per reproduction shape (error/wrong-result/crash/custom/stub), disk/teardown
  discipline, the rolling-restart timing caveat, the reliable cqlsh/system.log signal check, the
  multi-cluster fan-out recipe, and the Cassandra deploy-path blockers (ready-wait label, MCAC,
  5.0.x image tag, reproducer auth on both the workload AND direct paths, operator webhook, generated-code
  pod-label bug, multi-cluster `kind load --name`, and the crash_on_startup config-rendering
  non-manifestation) — most now fixed in db_build_spec.py and the build managers. Use when validating,
  debugging a deploy/inject of, or triaging runnability of generated DB problems.
user-invocable: true
---

# Runtime-validating SREGym DB-bug problems

Static checks (`py_compile`, `ProblemRegistry()` loads the class) only prove a problem *parses* and
*registers*. **Runtime validation** proves the problem actually *works*: the framework deploys the
stock cluster, swaps in the buggy image, runs the reproducer, and the bug becomes **observable on the
cluster**. This skill is the operational playbook for that, learned validating the 84 Cassandra
problems added in commit 37199d30 (pilot `auto_cassandra_15896` confirmed end-to-end). **Outcome: all 85
were executed end-to-end with this playbook — 37 manifested / 27 not-manifested / 21 stub-not-validatable;
the full per-problem verdict table lives in `validation-findings.md`.**

## Mental model

A generated problem (`GenericCustomBuildProblem` subclass) runs through the Conductor:
`__init__` (resolve version + build/re-tag image) → `app.deploy()` (stock 3-node K8ssandra cluster) →
`inject_fault()` (swap to buggy image → `setup_preconditions()` → run `reproducer` → if
`continuous_reproducer`, deploy a looping reproducer pod). Validation = run up to and through
`inject_fault()`, then **look at the cluster** for the documented buggy signature.

We do NOT need an agent to "solve" anything. We only need the deploy + inject to happen and the
symptom to appear. So use the **demo agent**, which needs no LLM / API key.

## The validation loop (demo agent, no API key)

```
uv run python main.py --agent demo --model gpt-5 --problem <pid>
```

- **Use `uv run`** — bare `python main.py` fails with `ModuleNotFoundError: rich`. The repo runs under uv.
- `demo` has `container_isolation: false` (agents.yaml) → runs on the host, no agent image build, no
  preflight API call (preflight only runs for stratus/claudecode/codex). `--model gpt-5` is just to
  satisfy argparse; the demo agent ignores it. **No API key needed.**
- `start_problem()` deploys the cluster and calls `inject_fault()` **before** the demo agent starts.
  The demo agent then idles waiting on file triggers `/tmp/next`, `/tmp/skip`, `/tmp/quit` — this
  keeps the run alive (Conductor will not tear down) so you have a **stable window to observe**.
- Run `main.py` as an async/background shell so you can `kubectl` against the cluster while it idles.
- When done observing: `touch /tmp/quit` to let the demo agent exit cleanly, then stop the `main.py`
  process (`kill <PID>`). **Always kill the demo/main.py process when finished with a problem** (user
  rule). Then tear down the namespace.

### Where the cluster actually lives (IMPORTANT)
The CR is named `sregym-cassandra` but the K8ssandra operator deploys the datacenter pods into the
**`k8ssandra-operator` namespace**, NOT a `sregym-cassandra` namespace. Probes:
```
kubectl get pods -n k8ssandra-operator | grep dc1            # 3× ...-dc1-default-sts-{0,1,2}
kubectl get cassandradatacenter dc1 -n k8ssandra-operator -o jsonpath='{.status.cassandraOperatorProgress}'
kubectl get k8ssandracluster,cassandradatacenter -A
kubectl logs deploy/sregym-cassandra-reproducer -n k8ssandra-operator --tail=50
kubectl exec sregym-cassandra-dc1-default-sts-0 -n k8ssandra-operator -c cassandra -- nodetool status
```

### Watch the deploy/inject progress
```
grep -E "DEMO AGENT ACTIVE|Buggy image active|Startup crash confirmed|deploy_continuous_reproducer|rolling" <run log>
```

### ⏱ Rolling-restart timing caveat (validate AFTER the cluster restabilizes)
The fault inject swaps in the buggy image, which the operator applies as a **rolling restart** of all 3
pods (~6–9 min on a loaded single-disk kind host; full first bring-up is ~8–13 min — the operator starts
nodes one at a time). **During the restart, RF=1 ranges transiently return
`Unavailable`/`WriteTimeout`/`ReadTimeout` (`alive_replicas: 0`) — this is NOT the bug.** Wait until:
```
kubectl get cassandradatacenter dc1 -n k8ssandra-operator -o jsonpath='{.status.cassandraOperatorProgress}'  # == Ready
kubectl exec ...sts-0... -- nodetool status   # all 3 nodes UN
```
then check the bug signal. On a stable cluster the documented signature is deterministic.

### ✅ Most reliable signal check (don't rely solely on the reproducer pod)
The in-cluster reproducer pod can stay stuck on transient post-restart timeouts (RF=1 + loaded host) and
may not cleanly flip. The deterministic check is to run the reproducer's own CQL **via a file** (avoids
shell-quote mangling of `INSERT … JSON`) against a stable node with superuser creds, and/or grep the
server log:
```
U=$(kubectl get secret sregym-cassandra-superuser -n k8ssandra-operator -o jsonpath='{.data.username}' | base64 -d)
P=$(kubectl get secret sregym-cassandra-superuser -n k8ssandra-operator -o jsonpath='{.data.password}' | base64 -d)
# write run.cql into the pod, then:  cqlsh -u "$U" -p "$P" 127.0.0.1 -f /tmp/run.cql
# authoritative server-side stacktrace:
kubectl exec ...sts-0... -c cassandra -- grep -A6 NullPointerException /var/log/cassandra/system.log
```

### 🤖 Automated batch validation — stabilize the ring BEFORE the direct re-run (Finding #11)

When validating many problems with a harness (deploy → inject → observe → capture → classify → teardown),
the naïve "capture right after `DEMO AGENT ACTIVE`" gives **false verdicts**. Two compounding causes, both
specific to `prebuilt_from_stock` injects, must be handled or you will record a spurious signal as the bug:

1. **Inject leaves BOTH operators scaled to 0** (the operator-override path). A pod deleted by the rolling
   restart is then **never recreated** (StatefulSet stuck at 2/3). **Fix: scale both operators back to 1**
   (`kubectl scale deploy cassandra-operator-cass-operator cassandra-operator-k8ssandra-operator
   -n k8ssandra-operator --replicas=1`) and wait for the datacenter to finish (below). This is blocker #6.
2. **The continuous reproducer pod hammers `DROP`/`CREATE` on the same keyspace while nodes restart** →
   **cross-node schema disagreement** (`ConfigurationException: Column family ID mismatch`) +
   `OperationTimedOut`/`Connection defunct`/`Keyspace … does not exist` cascade. None of that is the bug.
   **Fix: delete the continuous reproducer Deployment** (`kubectl delete deploy sregym-cassandra-reproducer
   -n k8ssandra-operator`) so DDL stops, then let the schema converge.

**The stabilization gate (poll until ALL of):**
```
kubectl get cassandradatacenter dc1 -n k8ssandra-operator -o jsonpath='{.status.cassandraOperatorProgress}'  # == Ready
kubectl exec <up-pod> -c cassandra -- nodetool status            # 3x "UN"
kubectl exec <up-pod> -c cassandra -- nodetool describecluster   # exactly ONE schema version line
```
Only when `Ready` + 3×UN + a single schema version is the ring clean. THEN run the reproducer directly via
`cqlsh -f` with superuser creds. **Pick a 2/2 pod with NO `deletionTimestamp`** (a "Ready" pod can be
mid-Terminating during the operator's rolling restart → `NoHostAvailable`), and **retry the direct re-run a
few times** re-picking the pod. On a clean ring only the genuine buggy statement errors — e.g. 15814 yields
exactly `InvalidRequest … code=2200 [Invalid query] message="Invalid list literal for version of type
frozen<list<int>>"` on the INSERT line and nothing else. Classify by **excluding spurious patterns**
(`NoHostAvailable|OperationTimedOut|Connection defunct|does not exist|No keyspace has been specified|
Unavailable|Cannot achieve consistency|timed out`) and requiring a documented-type server error
(`Error from server|InvalidRequest|code=\d{4}|ServerError|<Java exception>`). Working harness:
`/tmp/val-harness/validate_one.py` (this session).


The framework's file handlers (`logs/sregym_*.log`, `results/<ts>/.../sregym_*.log`) only capture the
`all.*` logger hierarchy. A problem's **own** logger (e.g. a custom `inject_fault`, the crash setup) and
all **pod logs** go to the **console only** — they are NOT in those files. So the headline signature
(e.g. a `custom_inject`'s `ConcurrentModificationException`, the wrong-result rows) can vanish:
- **Always redirect main.py stdout to a durable file**: `... uv run python main.py … > /tmp/val-logs/<pid>.log 2>&1`.
  Then `grep` THAT file for the signature. (A subagent's interactive shell scrollback is **discarded** when it
  finishes — do not rely on it; we lost a confirmed CME this way and had to re-run.)
- **For pod-log signals** (crash boot errors, server `system.log` stacktraces) dump them to a file
  *before* any teardown — teardown deletes the namespace and the pod logs with it:
  `kubectl logs -n k8ssandra-operator <pod> -c cassandra --tail=400 > /tmp/val-logs/<pid>-evidence.txt`
  (add `--previous` for a crash-looping pod). Verify with `grep` before deleting anything.

## Observable signal per reproduction shape

Categorize each problem first (read the file): `continuous_reproducer`, `expected_output`,
`crash_on_startup`, a custom `inject_fault`, or a `STUB`.

| Shape | How the bug manifests on the cluster | Validation = PASS when |
| --- | --- | --- |
| **error_continuous** (`continuous_reproducer=True`, no `expected_output`) | reproducer pod's probe runs the CQL; error → non-zero exit → pod **NotReady**; the verbatim exception shows in the pod logs | pod NotReady AND/OR the documented exception/ServerError in `kubectl logs` of the reproducer pod (or re-run the CQL via cqlsh and see it) |
| **wrong_result** (`expected_output` set to the BUGGY value) | probe greps for the buggy value; present → exit 0 → pod **Ready** | pod Ready AND the buggy value appears in the reproducer output |
| **crash_on_startup** (`crash_on_startup=True`) | buggy image swapped in, DB process crashes on boot | cassandra pod in **CrashLoopBackOff** / not Ready after swap; boot error in pod **`system.log`** (NOT stdout). ⚠ **Config-rendering crashes (CR `cassandraYaml`) may NOT manifest — see blocker #10.** |
| **custom_inject** (overrides `inject_fault`, often nodetool sequences) | whatever the custom method does (flush loops, nodetool, background workload) | the documented symptom from the evidence log appears (inspect per-problem). ✅ validated live on `21065`: `nodetool garbagecollect` → `ConcurrentModificationException` at `CompactionManager.java:691`. |
| **STUB_multinode** (`STUB` in file, `continuous_reproducer=False`) | the single-cluster CQL path cannot create the per-replica divergence / bootstrap-race / cross-version state | **Do NOT stop at "stub".** A stock-image **raw ring** (see below) reproduced **19 of 21** of these through the full framework. Only mark `stub-not-validatable` after a genuine raw-ring attempt, and then record a **precise** blocker (offline-tool / nondeterministic-timing / cross-version-sstableloader), never a generic "multi-node" note. |

## Dig deeper before declaring "not-manifested" (the verdict is usually an OBSERVABILITY bug)

On a **version-correct buggy image**, "deployed + injected, signature did not fire" is far more often a defect in
*how/when the problem observes* than evidence the bug is gone. All 6 dug-deeper problems (`14013, 14204, 15191,
18935, 19401, 20238`) had a real, present bug. Checklist before you write `not-manifested`:

1. **Wait for the ring to re-stabilize after the image swap.** Inject does a rolling restart; observing while the
   ring is mixed `UN`/`DN` or split-schema gives a transient `NoHostAvailable`/empty result. Gate on **all `UN` +
   a single `nodetool describecluster` schema version** before probing.
2. **Observe through the right surface.** `cqlsh` cannot run `nodetool` (use `kubectl exec`); a `CL=ONE` read is
   routed to an arbitrary replica (not a per-node probe — use `sstabledump`/`flush`); a server-only frame never
   reaches the client (scrape the pod **`system.log`**); an on-disk bug needs a `nodetool flush` first.
3. **Confirm the trigger actually armed.** `kill 1` under `cass-management-api` kills the mgmt API, not Cassandra
   (use the lifecycle endpoints); a CR `jvmOptions`/config patch sticks but a StatefulSet-env patch gets
   reconciled away; `nodetool repair` must *succeed* before a repaired-SSTable-mix bug can fire.

## Reproducing multi-node / cross-version bugs: the raw ring (`CassandraRawRing*`)

For bugs a single `reproducer` CQL string cannot express (per-replica divergence, bootstrap/decommission races,
cross-version streaming, multi-DC), use the reusable infrastructure built for exactly this — it stays inside the
benchmark's `deploy_app → inject_fault → oracle` contract:

- **`sregym/service/apps/cassandra_raw_ring.py` — `CassandraRawRingApplication`**: self-seeded headless Service +
  StatefulSet on the **stock** image (buggy version = fix patch − 1). API: `cqlsh`, `nodetool`,
  `disablegossip`/`enablegossip`, `disablehandoff`, `flush`, `wait_ring(n)`, `node_state`/`wait_node_state`,
  `launch_joiner(ring_delay_ms)` (parks a node in `UJ`/`BOOT`), `launch_daemon`/`kill_daemon`/`wipe_data`,
  `system_log`/`pod_logs`/`grep_log`, `pod_ip`, `bare_pod_manifest(set_seeds=False)` (for `replace_address`).
- **`sregym/conductor/problems/cassandra_raw_ring.py` — `CassandraRawRingProblem`**: subclasses
  `GenericCustomBuildProblem` (so it is **auto-discovered**, no registry edit) but overrides `__init__` to bypass
  the operator build. A concrete problem sets `cassandra_version`, `source_git_ref`, `ring_namespace` (unique),
  `replicas`, `root_cause_*`, and overrides `post_deploy()`, `inject_fault()` (`@mark_fault_injected`),
  `build_mitigation_oracle()`. `requires_openebs()` → `False`.
- **`sregym/conductor/oracles/cassandra_raw_ring_oracles.py`**: `CassandraWrongResultOracle` (greps a CQL result
  for the buggy value; `reestablish` hook) and `CassandraLogGrepOracle` (greps `system_log`/`pod_logs`/a re-run
  `command`; `retrigger` hook). **`success=False` ⇔ bug present.**

**Gold templates** (copy these): `auto_cassandra_15459.py` (gossip-isolation wrong-result — the cleanest:
`disablegossip` on the peer, write at `CL=ONE` `USING TIMESTAMP`, `flush`, then `CONSISTENCY ALL` `GROUP BY`),
`auto_cassandra_14559.py` (in-pod daemon log-grep), `auto_cassandra_14463.py` (self-seed + `replace_address`),
`auto_cassandra_16259.py` (cross-version **in-place upgrade** via an initContainer of the older patch on a shared
`emptyDir`), `auto_cassandra_16334.py` (**2-DC** ring, `NetworkTopologyStrategy{dc1:1,dc2:1}`).

**Proven techniques:** gossip-isolation divergence (verify physically with `sstabledump`, not a `CL=ONE` read),
`UJ`/bootstrap parking, `FatClient`/decommission gossip, `replace_address` self-seed, cross-version in-place
upgrade (initContainer of the older patch on a shared `emptyDir`), cross-version **`sstableloader`** zero-copy
streaming (a helper pod of the older version generates + flushes legacy sstables, then streams into the buggy
ring), a raw stdlib native-protocol-v4 client staged in-pod (for protocol/paging-state bugs), per-pod
ClusterIP topology with a stale `preferred_ip`, and multi-DC. Add `-Dcassandra.max_local_pause_in_ms=600000`
for deterministic gossip conviction on a loaded kind host. **A flattened single-CQL "reproduction" of a
multi-node bug compiles and registers but silently does NOT reproduce it — strictly worse than the raw ring.**

## Disk & teardown discipline (the binding constraint)

Each problem deploys a **3-node** K8ssandra datacenter (`cass-management-api`, ~600MB image ×3 nodes'
containerds) + cert-manager + openebs + 3×5Gi PVCs. On a small host disk this fills fast.

- **Reclaim before starting:** delete stale clusters, prune leftover host images
  (`docker images | grep cassandra` from earlier manual repro phases are NOT used by the runtime —
  the runtime uses `cass-management-api` loaded into kind), and recreate the kind cluster fresh for a
  clean, max-headroom baseline (`kind delete cluster` reclaims node volumes; recreate from
  `~/kind-config.yaml`).
- **Concurrency = 1** for full deploys here (a single 3-node ring is already heavy). Do not fan out
  parallel `main.py` runs against one kind cluster.
- **Tear down between problems:** `kubectl delete k8ssandracluster sregym-cassandra -n k8ssandra-operator`
  (this removes the datacenter + pods; then delete the PVCs `server-data-*` in `k8ssandra-operator` to
  free the 3×5Gi), and reclaim kind node image space between waves
  (`docker exec <kind-node> crictl rmi --prune`). Watch `df -h /`.
- cert-manager + operator can be left installed between problems (they're reused); only the cluster
  namespace must be cleared.

## Cassandra deploy-path blockers (found validating commit 37199d30)

Validating these problems surfaced **13 systematic blockers** that previously prevented *any* Cassandra
problem from completing a benchmark run, plus **1 root-caused non-manifestation** (#10), **1 silent
false-negative class** (#16, deploy-version-mismatch), **1 silent false-POSITIVE class** (#17, cqlsh
can't parse the `nodetool` lines in a custom reproducer → counted as a real error), and — in a sustained
fan-out — **3 image/PVC races that silently collapse throughput to ~20%** (#18 host/kind-node prune races,
#19 `kind load` aborting on the disk-full control-plane, and #4 upgraded to the dominant systematic
deploy-killer). Findings #1, #2,
#3, #5, #7, #8, #12 are fixed in problem code / `sregym/service/db_build_spec.py` (+ `generic_db_app.py`
for #1, + 15 problem files for #7, + base class & 9 files for #12); #9 and #19 are fixed in the build managers; #15 is fixed in kubectl.py;
#4, #6, #13, #14, and #18 are infra hazards auto-healed by the infra-monitor; #10
is a platform incompatibility documented (not fixable as-designed). Full writeup: `validation-findings.md`
at the repo root.

| # | Blocker | Scope | Status |
|---|---------|-------|--------|
| 1 | **Ready-wait label selector** — `_wait_for_cluster_ready` selected `app.kubernetes.io/instance={cluster}` but K8ssandra labels pods `cassandra-{cluster}` → 0 pods matched → every deploy timed out at 1200s even when healthy | all 85 | ✅ fixed (per-spec `ready_instance_label_fn` → `cassandra-{cluster}`) |
| 2 | **MCAC** — for `<3.11.13` / `4.0.0–4.0.3` the operator errors `MCAC cannot be disabled` and never makes a StatefulSet | 39 | ✅ fixed (version-gated `telemetry.mcac.enabled: true` in cluster manifest) |
| 3 | **5.0.x image tag** — `cass-management-api:5.0.x-ubi8` doesn't exist (only `-ubi`); `_nearest_released_version` keeps the bad suffix | 19 | ✅ fixed (per-spec `base_image_resolver` → `-ubi` for major≥5). NOTE `5.0.0-ubi` also missing → 5.0.0 may need a bump to 5.0.1 |
| 4 | **openebs orphan init-pvc** — provisioner crashes (exit 255) under I/O pressure, orphans `Completed` `init-pvc-<uid>` pods → on restart re-provision hits `pods "init-pvc-<uid>" already exists` → PVC `Pending` forever → 1200s deploy timeout → **every** subsequent problem error-earlyexits (THE dominant systematic deploy-killer in long fan-out: ~20+ in a row until cleared) | transient→systematic | ✅ auto-healed: monitor section 1b force-deletes any non-`Running` `init-pvc-*` pod each cycle (idempotent mkdir/chmod, safe). Healing only the provisioner pod is NOT enough — must delete the orphans too |
| 5 | **Reproducer WORKLOAD auth** — the long-running probe Job's `cqlsh` ran with no creds vs `PasswordAuthenticator` → `AuthenticationFailed`, false NotReady | ~29 | ✅ fixed (`_cassandra_reproducer_workload` injects `<cluster>-superuser` secret as `CASS_USER`/`CASS_PASS`, passes `-u/-p`) |
| 6 | **Operator webhook down** — if the cass/k8ssandra-operator Deployments are scaled to 0, the inject's rolling restart stalls with `FailedCreate … mpod.kb.io … connection refused` | transient | ⚠ workaround: `kubectl scale deploy cassandra-operator-cass-operator cassandra-operator-k8ssandra-operator -n k8ssandra-operator --replicas=1` |
| 7 | **Generated problem code uses the wrong pod label** — `setup_preconditions`/`inject_fault`/flush helpers do `kubectl get pods -l app.kubernetes.io/instance={cluster}` (should be `cassandra-{cluster}`) → 0 pods → silently skip `nodetool flush`/exec → flush-dependent bugs never reproduce | 15 files | ✅ fixed (prefix `cassandra-` in all 15) |
| 8 | **Reproducer DIRECT-PATH auth** — `_cassandra_run_reproducer` (`spec.run_reproducer_fn`, used by `setup_preconditions`/`run_reproducer` to SEED state) also ran `cqlsh` with no creds → setup CQL fails auth, schema/keyspace never created, so flush + later SELECT can't reproduce (distinct path from #5) | all setup_preconditions users | ✅ fixed (fetch `<cluster>-superuser` secret, pass shell-quoted `-u/-p`; falls back to no-auth) |
| 9 | **Multi-cluster `kind load`** — `kind load docker-image` ran with no `--name` → built/retagged buggy image landed in the default `kind` cluster, not the per-worker `kind-valN` → pods `ImagePullBackOff` at the inject image-swap | multi-cluster fan-out | ✅ fixed (`_kind_name_arg()` in both build managers derives `--name` from the active `kind-<name>` context) |
| 10 | **`crash_on_startup` config-rendering bug unmanifestable on k8ssandra** (`auto_cassandra_18778`): (a) `inject_buggy_image_expect_crash`→`_operator_override` scales BOTH operators to 0, so the `setup_preconditions` CR `cassandraYaml` patch never reconciles (`enabled:false` persists, no crash); (b) forcing operators up reconciles but the config-builder renders `keystore_password:cassandra` for the CR empty `""` → a *different* crash (`keystore password was incorrect` at `:158`, not the documented `:133`); (c) the cass-management-api keeps the **container** Running while the cassandra **process** fails (`restartCount:0`, pod `1/2`), so `_wait_for_crash_loop` (container-level) never trips → 300s timeout | `crash_on_startup` config bugs | ⚠ root-caused, NOT fixable as-designed (CR path can't express an empty keystore_password) |
| 12 | **Custom `inject_fault()` drives `cqlsh` without creds** — a THIRD auth path beyond #5/#8: ~half the `custom_inject` problems override `inject_fault()` and run `kubectl exec … -- bash -lc 'cqlsh …'` on the server pod themselves, with no `-u/-p` → first setup statement fails `AuthenticationFailed('Remote end requires authentication.')` → schema/data never created → bug never fires. Proven on `16071` (SASI-OOM never triggered, pods stayed `2/2`) and silently broke `10968`'s conclusion | 9 real customs: `10968, 13935, 14204, 15134, 15191, 16071, 18760, 19747, 20036` | ✅ fixed (base-class `_cqlsh_auth_flags()` + `_authed_cqlsh()` regex-inject auth into every `cqlsh` form; each file routes its in-pod cmd through it) |
| 13 | **`~/.kube/config` current-context unset → every run crashes at Conductor init** — `KubernetesAPIProxy.__init__` (`k8s_proxy.py:87`) hardcodes `~/.kube/config` and IGNORES `KUBECONFIG`. Deleting *any* kind cluster makes kind run `kubectl config unset current-context`, so if the deleted cluster was the current-context (or it was the last delete), `~/.kube/config` loses `current-context` → `ConfigException: Expected key current-context` → main.py dies in ~27s for ALL problems (mass `error-earlyexit`) | multi-cluster fan-out (any kind delete) | ⚠ workaround: keep `~/.kube/config` pointed at a live cluster (`cp /tmp/kubeconfig-valN ~/.kube/config`); infra-monitor restores it automatically when current-context goes missing |
| 14 | **containerd orphaned-snapshot leak fills the disk every ~12–15 problems** — each `inject_fault()` `kind load`s a new buggy image into every node's containerd; the overlay snapshots accumulate (~0.5 GiB/problem × N nodes) and are **NOT** GC'd by `crictl rmi --prune` alone (measured 9.8G→9.7G = nothing). On a 63 GiB host, Docker "Local Volumes" creep 32→41 GB over ~15 problems until **disk hits 0** → deploys fail + openebs crash-loops. Also `/tmp/sregym-sources` accumulates one shallow clone per buggy version (~160–360 MiB each; never auto-cleaned) | any multi-problem run on one kind cluster | ⚠ workaround: the ONLY effective reclaim is `systemctl restart containerd` on each **WORKER** node (forces snapshot GC) **then** `crictl rmi --prune` — reclaimed 0→21 GiB in one shot. NEVER restart the control-plane's containerd (disrupts etcd/API → openebs provisioner loses its leader lease → crash-loops 30–60 min). **Three gotchas:** (1) at *exactly* 0 disk the restart fails (`DeadlineExceeded`, reclaims nothing) — `rm -rf /tmp/sregym-sources/*` FIRST to get headroom, then it reclaims 0→21G; (2) a fresh ring's 3×5 GiB PVCs can drop disk 7G→0 *inside one deploy*, so fire heavy-GC at **<12 GiB** (`df -BG` rounds UP, so a `<10` test never fires at 9.7G actual=shows 10G) with sources cleaned first; (3) **`kind get nodes --name` MUST use the EXACT cluster name** — a cluster created `kind-val1` needs `--name kind-val1`; `--name val1` returns no nodes and the reclaim loop is a **silent no-op** (the leak grew to 32.87 GiB this way before it was caught). The kube-context is `kind-kind-val1`, so `${ctx#kind-}` → `kind-val1` is the correct derived name. `crictl rmi --prune` on the correct nodes is non-disruptive and reclaimed 10G→13G alone; reserve the disruptive containerd restart for snapshots. **(4) heavy-GC must ROLL one worker node at a time, waiting for the ring to return to 3×`2/2` between nodes** — restarting all workers at once kills all 3 ring pods and an in-flight reproducer sees spurious `Unavailable`/`Cannot achieve consistency`/`Bad credentials … LOCAL_QUORUM` → a FALSE verdict (cost `auto_cassandra_18935` a requeue). Run the non-disruptive light prune eagerly (`<16G`) so the rolling heavy-GC (`<11G`) rarely fires. Infra-monitor does all this (worker nodes only, 600s cooldown); pair with requeue cap ≥4 |
| 15 | **`wait_for_ready()` hangs on Completed `cleanup-pvc-*` pods → blocks EVERY deploy** — `kubectl.py:191` counts a pod ready only if `all(cs.ready)`, which a `Succeeded`-phase (Completed) pod never satisfies. openebs leaves a `cleanup-pvc-<uuid>` `0/1 Completed` pod after every teardown; once one lingers, the next problem's `wait_for_ready("openebs")` can never reach `len(ready)==len(all)` → 600s timeout → `error-earlyexit`, cascading to all subsequent problems (looks like a provisioner outage) | any multi-problem run (triggers after the first teardown) | ✅ fixed (`wait_for_ready` now treats `Succeeded`/`Failed` pods as ready, matching `is_ready`/`wait_for_stable`); immediate unblock = `kubectl delete pod -n openebs --field-selector status.phase==Succeeded` |
| 16 | **Version resolver silently deploys a FIXED image when the buggy tag is missing → false-negative** — `_nearest_released_version` (`generic_custom_build.py:53`) deploys the exact buggy `cass-management-api:<ver>` when `docker manifest inspect` finds it, else falls back to **newest same-`MAJOR.MINOR`, else latest-stable** — which for a missing buggy tag is a **FIXED** build, so the bug can't manifest yet the run reports a clean `not-manifested`. Only buggy versions `3.11.6, 3.11.9, 3.11.10, 5.0.0` lack a tag (use `docker manifest inspect`, NOT the paginated tag-list API which hides tags past ~page 8); they deploy FIXED `5.0.8`. Affected: `10968`(3.11.6), `14925`(3.11.9), `12949`(3.11.10), `19880`(5.0.0) | 4 problems | ⚠ documented (not changed mid-run): re-label these `deploy-version-mismatch`. **Review rule: for EVERY verdict grep the log for `Deploying cassandra <X> via` and confirm `<X>==db_version`.** Recommended fix: prefer nearest patch **≤ buggy** same-minor (preserves the bug); `3.11.6`/`5.0.0` have no lower tag → genuinely `blocked-no-image` without a source build |
| 17 | **`classify()` scores cqlsh-can't-parse-`nodetool` as a real bug signature → false-POSITIVE** — the harness re-runs a problem's `reproducer` via `cqlsh -f` as a clean probe, but many `custom_inject` reproducers contain `nodetool`/shell lines. `cqlsh` emits `SyntaxException: line 1:0 no viable alternative at input 'nodetool'` + `Invalid syntax at line 1, char 1`, which `REAL_ERR` matched → false `manifested`. The cqlsh parse error is an artifact, NOT the bug; the real signal for a custom is the problem's OWN inject-phase marker. Hit on `auto_cassandra_17752` (the join_ring=false bug can't even occur on an operator ring) | custom_inject with nodetool/shell reproducers | ✅ fixed (`validate_one.py` adds `CQLSH_JUNK` + `_strip_junk()`; `classify()` strips cqlsh-non-CQL lines before `REAL_ERR` in `error`+`custom` modes). **Review rule: every custom `manifested` verdict must be confirmed against the inject-phase `<pid>.log` marker, never the direct cqlsh re-run** | **Hardened (cycle 18):** `CQLSH_JUNK` only strips known shell keywords (`nodetool`/`bash`/`sstable`…), so a STUB reproducer carrying embedded **Java/prose** (e.g. `auto_cassandra_15857`, an offline `CQLSSTableWriter` bug "NOT visible from cqlsh/a live server") still produced cqlsh `SyntaxException`s on arbitrary words (`'silently'`/`'import'`/`'public'`) → false `manifested`. Can't blanket-strip `SyntaxException` (`17919`'s IS the documented bug). Fix: `validate_one.py` adds `STUB_RE`/`is_stub()` — a reproducer whose first ~800 chars start a line with `STUB` (opt. `-- ` prefix) is multi-node/offline/cross-version → `classify()` short-circuits to `not-manifested|stub` (deploy still happened = executed). **Rule: a STUB reproducer is `not-manifested|stub` regardless of any cqlsh re-run output** |
| 18 | **Image-prune races silently collapse throughput to ~20%** — under sustained disk pressure the every-cycle disk guard fires constantly and deletes the just-built image: (a) **host** `docker image prune -af` removes the freshly re-tagged `sregym/cassandra-patched:<ver>` (tagged but used by no host container) → `kind load … not present locally`; (b) **kind-node** `crictl rmi --prune` removes the *loaded* image while its pod is still `Pending` (image "unused" for the whole PVC-binding window) → `ImagePullBackOff`. The build log says `Loaded … into kind cluster` yet the image is on no node moments later. Net: deploy/rollout timeouts → `error-earlyexit` on ~every problem | any sustained fan-out with an aggressive prune threshold | ✅ fixed in infra-monitor: host prunes use `--filter "until=10m"`/`until=30m` (protect fresh artifacts). **The kind-node prune gate was first tried as a ring-readiness check (`2/2 Running`) but that is WRONG** — a prebuilt buggy image is built+loaded in `__init__` *before* deploy, and the ring stays `2/2` on the **STOCK** image for the whole deploy→inject window, so the gate opens while the buggy image is still "unused" → prune deletes it → `ImagePullBackOff` on inject (seen on 17919/19401). **Correct gate: skip the kind-node prune whenever a `main.py --agent demo` problem is in flight** (`ps -eo cmd \| grep -c "[m]ain.py --agent demo"`) — only prune *between* problems. Same `inflight==0` gate on the heavy-GC. Verified on 19401/19637: buggy image stayed on all 3 workers through inject |
| 18c | **Per-problem deep disk reclaim** — because the kind-node prune is now gated off during a problem, reclaim deterministically at teardown. `crictl rmi --prune` drops image refs but does NOT reclaim the orphaned overlay snapshots from the image swap (Finding #14) — only a **containerd restart** does. `validate_one.py:prune_images()` (runs after teardown, even on `error-earlyexit`) restarts containerd on each **worker** node (control-plane excluded). Verified: worker `/var/lib/containerd` 6–8G→~4G each, host free **3.7G→12G** in one teardown. Makes the disk loop **self-correcting**: a low-disk failure triggers the reclaim, and its requeue then succeeds | any single-cluster sustained run on a tight disk | ✅ added to `validate_one.py:prune_images()` |
| 20 | **Custom `inject_fault()` verifies state on an unstable post-swap ring → spurious failure** — a custom diagnosis-only problem (19401, flat-path `nodetool import` silent-no-op) ran its full reproduction in `inject_fault()` then its "verify table empty" `SELECT` failed with spurious `Cannot achieve consistency ONE` because the verification runs *immediately* after the buggy-image rolling restart, before the ring is back to 3×`UN`. The bug is likely present but not cleanly observable. **Do NOT** edit the problem to add a ring-wait (that tunes the benchmark to pass) | custom diagnosis-only problems that assert state in `inject_fault()` | ⚠️ disposition rule: inject-phase log shows only spurious post-swap errors + no documented signature → **not-manifested** (inject ran, manifestation not observable); inject-phase log shows the exact documented signature (e.g. 18264 `FileAlreadyExistsException @ CustomClassLoader`) → **manifested** |
| 19 | **`kind load` aborts on the disk-full control-plane** — `kind load docker-image` copies into **every** node and returns non-zero if **any** fails. The control-plane is `NoSchedule` (runs no ring pods) yet every load still copied the buggy image there, accumulating `import-*` snapshots to **16 GiB** that can't be reclaimed without restarting etcd. Once its FS is tight the per-node load there fails → whole command aborts → image on **no** worker → `ImagePullBackOff` → rollout/deploy timeout | any multi-node kind cluster after enough loads | ✅ fixed in `generic_db_build_manager.py`: `_kind_worker_nodes_arg()` scopes `kind load` to `--nodes <workers>` (control-plane excluded) — skips the failing node AND stops the control-plane snapshot leak at its source. Fail-safe to load-all when workers can't be determined | **Reclaiming the existing control-plane pile (cycle 19):** the leak is fixed going forward, but a control-plane that already hoarded `import-*` snapshots stays full. `crictl rmi --prune` on the **control-plane** is **always safe even mid-problem** (it runs no ring pods, so it can never delete a buggy image a worker needs) — unlike the worker-node prune, which MUST be gated on `inflight==0`. The monitor gates *all* kind-node prunes on `inflight==0`, so the control-plane pile is never reclaimed while a problem runs and host disk can dip below the next config-gated problem's deploy budget. Fix: a tiny orthogonal detached loop (`cp_prune.sh`, PPID 1) runs `docker exec <cp> crictl rmi --prune` every 3 min when disk <14G — recovered host 6.6G→12G in one pass, non-disruptive (no containerd/etcd restart) |
| 21 | **Config-precondition problems are unarmable under the `prebuilt_from_stock` operator-override inject → structural false-negative** — a problem whose `setup_preconditions()` patches the **K8ssandraCluster CR** (e.g. enabling a `cassandra.yaml` `startup_checks`/`guardrails` block) records a clean `not-manifested`, but the inject log shows the CR patch was **rejected**: `cassandraYaml patch failed: ... failed calling webhook "vk8ssandracluster.kb.io": ... connect: connection refused`. Cause: `prebuilt_from_stock` `inject_buggy_image()` scales **both** operators (incl. `k8ssandra-operator`, which serves the admission webhook) to **0** to patch StatefulSets directly; the subsequent `setup_preconditions()` CR patch then hits a dead webhook → the precondition is never armed → guaranteed not-manifested **by construction**. Distinct from #6 (transient); this is **deterministic** — the precondition window is inside `inject_fault()` with the operator already at 0, so the harness's later scale-to-1 is too late. Confirmed on `21348` (5.0.8, `startup_checks`→SettingsTable `ClassCastException`) | config-precondition problems with `prebuilt_from_stock=True` | ⚠ disposition: classify **not-manifested** with note *"precondition unarmable: inject scaled operator→0, CR/webhook patch refused"*; do **not** requeue (deterministic). Real fix would re-scale `k8ssandra-operator` to 1 or patch the CR before the swap — out of scope for validation |
| 21b | **`crash_on_startup` heuristic must check the DOCUMENTED signature, not "any startup exception"** — the classifier marked `20787` (5.0.4 guardrail-ordering crash) `manifested|crash` on a generic `CassandraDaemon.java:887 - Exception encountered during startup`, but the documented signature (`Cannot get data directories grouped by file store` / `NoSuchFileException` / `DiskUsageMonitor`) was **absent**. The captured crash was a `LogReplicaSet.java:96 - Failed to create log replica` with a **doubled path** → `disk_failure_policy: stop` — a spurious artifact of `setup_preconditions()` **deleting the PVC data dir** on a live node (corrupts compaction txn logs); sts-0/1 even recovered to `2/2`. Reinforces #10: config-rendering / data-dir-precondition startup bugs are generally unmanifestable on the operator runtime | any `crash_on_startup` problem | ⚠ review rule: **manifested only if the crash carries the documented exception class+frame** (grep the evidence/`system.log` dump); a generic startup exception with the cluster recovering → **not-manifested** + spurious-crash note |
| 22 | **META: custom reproducers authored for a bare `cassandra:X` image often can't arm on the `cass-management-api` operator runtime → dominant `custom_inject not-manifested` cause** — the buggy binary is present and version-matched, but the *trigger* can't be staged because the operator image differs from the bare single-node image the evidence log used (non-root user, read-only `/`, no importable in-pod DataStax driver, different entrypoint/FS, host resource limits). Confirmed: `17136` (`mkdir /trap: Permission denied` → FQL trap unstageable), `17623` (`ModuleNotFoundError: No module named 'cassandra'` → unsorted-map native bind never ran), `16071` (SASI `OutOfMemoryError: Map failed` didn't fire — `vm.max_map_count` not exhausted), `14013` (`kill 1` hits the management-api PID 1, not cassandra → ring stuck `UN=0`), `14204` (`nodetool repair rc=2` on the ring → no repaired-SSTable mix → `garbagecollect` AssertionError never arms), `15191` (server-log-only `CorruptSSTableException` frame nobody scrapes + post-in-place-restart JMX `7199 connection refused` so "keeps serving" is unconfirmable), plus #21/#21b (webhook/data-dir). The cqlsh re-run is doubly useless here (it's junk for nodetool/native reproducers) | `custom_inject` problems with root-FS traps, in-pod Python drivers, mmap/resource triggers, or CR/webhook preconditions | ⚠ review rule: read the inject-phase `<pid>.log` and split (a) *trigger armed, bug genuinely absent* (rare) vs (b) *trigger couldn't be armed* (common: `Permission denied`/`ModuleNotFoundError`/`rc=1`/`webhook refused` on the setup step). Both → `not-manifested`, but (b) means the problem needs re-authoring against the operator container. Pure-CQL / `nodetool` / `kubectl exec`-against-the-ring reproducers DO work (every manifested custom used those) |
| 23 | **`post_deploy` config patch uses `kubectl patch --type=merge` on the `datacenters[]` ARRAY → webhook rejects → every datacenter-level config-gated problem `error-exit`s before observe-point** — config-gated problems flip a `cassandra.yaml` key (`enable_materialized_views`/`enable_user_defined_functions`/`enable_sasi_indexes`/`authenticator`) in `post_deploy()` by patching the live `K8ssandraCluster` CR. A JSON **merge** patch (RFC 7386) treats arrays as **atomic**, so `--type=merge` on `spec.cassandra.datacenters` **replaces the whole array** with a 1-element array missing the required `size`/`storageConfig`/`resources` → webhook rejects `… datacenters[0].size: Required value` → `kubectl` rc≠0 → `subprocess.run(check=True)` raises → `main.py` crashes pre-`DEMO AGENT ACTIVE` → `error-earlyexit`, requeued forever (16898 hit requeue 6; 16836/16977 5; 15134/16902 4 — the dominant earlyexit loopers, ~5 host-hrs wasted, 0 verdicts). Proven by `kubectl patch --dry-run=server`: merge→rejected, `--type=json`→accepted with siblings intact | the 7 datacenter-level config-gated problems: 16898/16977/16902/20171/15135/15134/16836 | ✅ fixed in all 7: `--type=json` JSON-Patch `{op:add, path:/spec/cassandra/datacenters/0/config/cassandraYaml, value:{…}}` (parent `…/0/config` exists from the CR template's `jvmOptions`, so `add` creates `cassandraYaml` cleanly, touching no siblings); also bumped `_wait_for_cluster_ready(600→1800)` (framework default is 1200; 600 too short for a 3-node rolling restart under disk pressure) + reset their requeue counters. The **non-broken** siblings 17266/17933 already patch the **cluster-level** `spec.cassandra.config.cassandraYaml` *map* (merge-safe) and were never affected — generator should prefer that path or emit `--type=json` for per-datacenter patches. **VALIDATED LIVE (cycle27): `20171` (CassandraAuthorizer auth-gated) deployed cleanly with the fix — reached `DEMO AGENT ACTIVE` with NO `datacenters[0].size` rejection, then the inject-phase authed reproducer fired the EXACT documented `code=2200 "Resource <keyspace system_views> doesn't exist"` signature → manifested. Confirm config-gated verdicts via the inject-phase `<pid>.log` marker, NOT the harness cqlsh re-run.** |
| 24 | **k8ssandra dropped the `-ubi8` image suffix at 4.1.10 too (not just 5.0.x) → Finding #3's static `major>=5` heuristic mis-resolves 4.1.10/4.1.11 to a 404 base image → `prebuilt_from_stock` problem fails in `__init__` forever** — `_resolve_base_image()` raises `RuntimeError: Base image 'k8ssandra/cass-management-api:4.1.11-ubi8' not found on Docker Hub and no Dockerfile fallback available` for a buggy-4.1.10 prebuilt problem, requeued forever with 0 verdicts. Docker Hub probe: `cass-management-api:4.1.9-ubi8` EXISTS but `4.1.10-ubi8`/`4.1.11-ubi8` are MISSING, while `4.1.10-ubi`/`4.1.11-ubi` (and bare tags) exist — the suffix scheme changed **mid-4.1.x**, not at the 5.0 boundary Finding #3 assumed. A `prebuilt_from_stock` problem has no usable source-`Dockerfile` `FROM` fallback (the clone's `FROM` is a JDK builder, not a runnable Cassandra image), so it hard-fails; a real-source-build problem of the same version (e.g. `21290`, 4.1.11) survives via its Dockerfile-`FROM` fallback | `prebuilt_from_stock` problems on **4.1.10 / 4.1.11** (here: `21057`) | ✅ fixed in `_cassandra_base_image()` (`db_build_spec.py`): replaced the static `major>=5 ? ubi : ubi8` with a `@functools.cache`-d Docker Hub **probe** that returns the first existing suffix (era-preferred order, then bare, then static fallback when offline). Verified `4.1.10/4.1.11 → -ubi` (fixed) and every previously-correct version unchanged (`4.1.9/4.0.1/3.11.7 → -ubi8`, `5.0.x → -ubi`). The prebuilt re-tag now succeeds and deploy uses `serverImage: sregym/cassandra-patched:4.1.10-…` so the operator never pulls a `-ubi8` tag. **Lesson: never encode image-suffix conventions as a version cutoff — registries change schemes mid-minor; probe for the tag that exists** |
| 25 | **`inject_buggy_image` rollout wait (600s default) too short for a 3-node Cassandra rolling restart on a disk-constrained host → non-deterministic `error-earlyexit` at inject** — `prebuilt_from_stock` deploys the STOCK base image (`k8ssandra/cass-management-api:<ver>-<suffix>`, confirmed via live `kubectl get pod -o jsonpath '{.spec.containers[0].image}'`) and `inject_buggy_image` swaps to the re-tagged `sregym/cassandra-patched:<ver>-<hash>` — deploy-name ≠ inject-name, so the swap is a **REAL 3-node rolling restart, NOT a no-op** (observed `sts-2`→patched `1/2` starting, `sts-0/1` still stock). `_wait_for_image_rollout` returns on the FIRST Ready pod (highest STS ordinal), but under disk/IO constraint a single Cassandra pod bootstrap can exceed 600s → `RuntimeError: Timeout (600s) waiting for image … to roll out` → earlyexit before observe-point. Non-deterministic: 16898/16836/20171 won within 600s and manifested; 16977 lost every attempt | `prebuilt_from_stock` problems whose inject rolling restart is slow under disk pressure (16977, 16898 early attempts) | ✅ fixed: `inject_buggy_image` → `_wait_for_image_rollout(image_tag, timeout=1200)` (was the 600s default); ALSO bumped the harness `DEPLOY_TIMEOUT 30→42 min` (its wait-for-`DEMO AGENT ACTIVE` budget must exceed deploy ~12m + post_deploy ~6m + inject up to ~20m, else it `timeout-deploy`s before the bumped inject window). Fast rollouts still return early (no slowdown), safe for all DBs. **Lesson: a non-deterministic timeout at a FIXED wall-clock limit = under-budgeted timeout, not a problem defect; and a `prebuilt_from_stock` swap is REAL (stock→re-tagged), so verify the running image before assuming a no-op short-circuit (it would be dead code — reverted one for exactly this)** |
| 26 | **Diagnosis-only problems whose bug is an internal STATE (gossip/ring/schema/repair), not an error, need un-truncated decisive-field logging — else the harness reads `review\|no signature` and the manifestation is unobservable** — `auto_cassandra_21057` (4.1.10 disk-usage guardrail) deployed the buggy ring and ran the full `nodetool` guardrail sequence, but its bug is that gossip `DISK_USAGE:<gen>:FULL` PERSISTS after the guardrail is disabled (buggy `DiskUsageMonitor` short-circuits `if(!enabled)return`; fix → `NOT_AVAILABLE`). The harness `REAL_ERR` classifier matches server *errors*, so a persisted gossip *state* never matched → `review\|no clear signature`. Worse, the problem's `_exec_nodetool` truncated `nodetool gossipinfo` stdout to `[:300]` and the per-node `DISK_USAGE` field sits AFTER `STATUS/LOAD/SCHEMA/…` → past the cut → the decisive field was never logged → manifestation unobservable even though it occurred | diagnosis-only STATE-persistence problems (21057; generalizes to any membership/metadata-state bug) | ✅ added `_log_disk_usage_state(pod,label)` to 21057 that greps `gossipinfo` for the `DISK_USAGE` lines and logs them UN-truncated at both observe points; re-run captured the live signature (after-tick `DISK_USAGE:257:FULL` armed → disable → after-disable STILL `FULL`) → manifested. **Rule: for a STATE bug, explicitly extract+log the decisive field un-truncated at the observe point; a raw truncated dump hides the signal from both a human reviewer and any error-regex classifier. Review STATE verdicts via that field, not the cqlsh re-run.** |

> **THREE separate cqlsh paths need auth (#5 + #8 + #12):** the *workload* Job
> (`_cassandra_reproducer_workload`, the continuous probe), the *direct* framework exec
> (`_cassandra_run_reproducer`, what `setup_preconditions`/`run_reproducer` use to seed state), AND a
> problem's **own** overridden `inject_fault()` doing `kubectl exec … cqlsh` on the server pod (#12).
> Fixing only the first two leaves the ~9 custom-inject problems' setup CQL failing auth. Symptom of the
> #8 gap: deploy succeeds, but logs show the setup CQL failing auth and later `keyspace <ks> does not
> exist` / "Flushing … on N pod(s)" with nothing actually flushed. Symptom of #12: the inject log shows
> `AuthenticationFailed('Remote end requires authentication.')` and the pods never change state. When a
> custom problem overrides `inject_fault()`, route its in-pod commands through the base-class
> `self._authed_cqlsh(inner)` (or add `-u/-p` from `self._cqlsh_auth_flags()` inline).

> **Watch for this when validating any problem with a custom `inject_fault`/`setup_preconditions`:** if
> its logs say "No … pods found … skipping", the label/namespace lookup is wrong and the precondition
> didn't run — the bug will look like it "didn't manifest" for a spurious reason. The correct cassandra
> pod label is `app.kubernetes.io/instance=cassandra-{cluster_name}`; the namespace is `k8ssandra-operator`.

- **multi-node STUBs (26):** see table — not validatable on a single cluster by design; mark
  `stub-not-validatable`, do not count as fail.

## Multi-cluster fan-out recipe (concurrent validation)

To run several `main.py --agent demo` validations at once you need **one kind cluster per worker** plus
**port/path isolation** (the framework's defaults collide). The code now reads these from env
(`setdefault`/`getenv`), so per worker export a distinct set before launching:

```
export KUBECONFIG=<per-worker kubeconfig>      # its own kind cluster
export API_PORT=<8000+n>                        # Conductor HTTP API (main.py)
export MCP_SERVER_PORT=<9954+n>                 # MCP server (mcp_server.py)
export K8S_PROXY_PORT=<16443+n>                 # k8s filtering proxy (conductor.py)
export DEMO_TRIGGER_DIR=<unique dir>            # demo agent /next,/skip,/quit (clients/demo/driver.py)
# (API_HOSTNAME/MCP_SERVER_URL derive from the above; agent kubeconfig path is suffixed by K8S_PROXY_PORT)
```

- **Name each worker kind cluster with "kind" in the name** (e.g. `kind-val1`): `_ensure_kind_cluster`
  (main.py) returns early if the current context name contains `kind`, so it won't try to auto-create.
- **Raise the inotify instance limit before creating a 2nd cluster.** Each kind node runs systemd and
  consumes inotify instances; with the default `fs.inotify.max_user_instances=128`, creating a second
  4-node cluster fails with `could not find a log line that matches "Reached target .*Multi-User
  System.*|detected cgroup v1"` and rolls back. Fix: `sudo sysctl -w fs.inotify.max_user_instances=1024`
  (and keep `max_user_watches` high). After that, back-to-back cluster creates succeed.
- Give each worker its **own kubeconfig file**: `kind get kubeconfig --name <c> > /tmp/kubeconfig-<c>`
  then `export KUBECONFIG=/tmp/kubeconfig-<c>` in that worker. Cassandra needs 3 schedulable nodes, so
  use the full 4-node `~/kind-config.yaml` per worker.
- **Custom/retagged images now load into the right worker cluster (Blocker #9, fixed).** `kind load
  docker-image` defaults to the cluster named `kind`; the build managers now derive `--name <cluster>`
  from the active `kind-<name>` context. If you build/load an image **manually** during validation, you
  must pass `--name <worker-cluster>` yourself (e.g. `kind load docker-image <img> --name kind-val2`) or
  the pods will `ImagePullBackOff` against an image that's only present on the default cluster.
- **Disk is the cap, not CPU.** Each 3-node deploy is ~10–12G; a ~30G-free host realistically holds
  **2–3** concurrent clusters. Use a worker pool (not unbounded), reclaim between waves, stop early if
  headroom drops. Each worker tears down its own `k8ssandracluster` + PVCs when done.
- Per-problem done-markers (a SQL row / file) make the fan-out resumable.

## Pilot-gate, then proceed

Run ONE simple `error_continuous` problem (3.11.x / 4.0.x / 4.1.x — a version whose `-ubi8` image
exists) end-to-end first. Time the deploy, confirm the signal mechanics and teardown, refine this
skill, THEN proceed problem-by-problem. Track each in a durable store (SQL table / file) with a
per-problem done-marker so the run is resumable.

## Honesty bar

Confirm the **verbatim** documented signature against the live cluster (pod logs / cqlsh output) —
do not infer "manifested" from a NotReady pod alone (it could be NotReady for an unrelated reason
like the auth or image-tag blocker). A healthy cluster with a quiet reproducer pod is a FAIL to
manifest, which is itself a valid, important finding to record.

For multi-node raw-ring reproductions the bar is the same: the reproduction only counts when the
**verbatim** signature is captured **through a real framework run** (`main.py --agent demo`) and the
oracle grades `Mitigation.bug_present=True` (`success=False`), with the on-disk evidence written to
`/tmp/fleet/evidence-<pid>.txt`. Static `py_compile` + registry-load is necessary but **not** sufficient.

## Disposition: manifest, remove, or honest-stub (the fleet decision)

After executing a problem, pick exactly one:

- **manifest** — the signature fires live; keep the problem (rework its observe-point/mechanism per the
  dig-deeper checklist if needed, then re-run to confirm).
- **remove** (`git rm`) — the bug is **non-viable on this runtime/architecture**, so a kept problem would be a
  misleading always-pass. Remove `crash_on_startup` (the `cass-management-api` keeps the container `Running`
  while the JVM fails — structurally unobservable), version-mismatch deploys-a-fixed-image, un-armable
  preconditions, not-observable-by-design (DEBUG-only/internal-refinement), and corrected false-positives.
- **honest stub** — the bug is real but cannot be staged even with the raw ring; keep the file as a clearly
  marked stub and record a **precise** blocker (offline-tool / nondeterministic-timing-window /
  cross-version-sstableloader / in-JVM-dtest-only), never a generic "multi-node" note. Attempt the raw ring
  **before** concluding this.

⚠️ **Sub-agent reporting under content filtering:** long verbose Cassandra stacktraces in a sub-agent's final
report can trip the response content filter (the message returns empty). Instruct fan-out agents to **persist
every result to disk as they go** (rewrite the file, write the evidence file, run the SQL `UPDATE`, mark the
todo done) and keep the final chat reply to **one terse line per pid** — then verify each agent's outcome from
SQL/disk/evidence, not from the returned message.

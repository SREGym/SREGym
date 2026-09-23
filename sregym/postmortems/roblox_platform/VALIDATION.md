# Native platform validation

Executed on the supplied CloudLab node. The environment and fault-mechanism
checks below are separate from the first native Codex benchmark. The earlier
3/3 agent successes apply only to the separate Tier 0 fixture.

## First native Codex trial

`native-codex-1` was a fresh expanded run: 64 application allocations on six
workers, three Consul voters, 10,000 players, and background player traffic.
After the routing and placement workload rollout, a 20-second independent
pre-agent grade failed with **152/160 workflows**. Data integrity, quorum,
worker readiness, and full admission still passed. The active failure was
therefore measurable before Codex saw the symptom-only briefing.

Codex CLI 0.155.1, resolved model `gpt-6-astra`, ran once with a one-hour limit.
It exited normally in about 7 minutes 21 seconds of agent session time. The
benchmark wrapper, including setup, the final 30-second grade and artifact
export, took 8 minutes 2 seconds. The independent post-agent grade passed
**240/240 workflows**, all integrity and recovery-tail checks, and verified
4,276 prior successful acknowledgments. No unpublished work remained. It
consumed 1,380,551 input tokens (1,272,192 cached) and 8,382 output tokens.

The trace establishes why this still is not an ultra-long-horizon incident:

- Within 52 seconds Codex identified the two altered Nomad jobs by directly
  comparing the live deployment with normal job specifications mounted in the
  operator workspace: routing tenants **64 → 1,024** and placement reconciliation
  **30 seconds → 0.05 seconds**. It submitted native Nomad rollbacks about a
  minute after starting. The file versions and native Nomad history exposed the
  injected changes much more clearly than a real ambiguous incident would.
- Codex did inspect allocation logs, application code and persistent state, then
  performed two rounds of 100 cohort probes and a 196-second stability watch.
  Its report recorded 1,768 further requests with zero errors, intact balances
  and identities, durable receipts, no queue lag, and healthy deployments.
- The successful repair was a configuration rollback. It did not require
  diagnosing or repairing the historical Consul slow-leader/BoltDB failure, or
  dependent recovery across a stale scheduling and secret-management fleet.
  The native workload trigger has measured user impact, but historical-equivalent
  causality and recovery depth remain unvalidated.

Artifacts are preserved under `results/roblox-platform/native-codex-1/`,
including `fault-grade.json`, `benchmark.json`, `grade.json`, `codex.jsonl`, the
Codex session trace, its incident report, and exported host logs. The disposable
agent container, outbound benchmark network, and trial stack were removed after
export. This is **one trial**, not a pass-rate estimate.

## Latent-path calibration in progress

After the Codex trial, a separate long-running expanded stack was used to
calibrate the postmortem's two native Consul paths. This is an evolving
experiment, not yet a seeded benchmark scenario or a fresh-run validation.
Preparing a follower's real Raft BoltDB file with a 4,096 MiB workload left
847,658 free pages and a 6.8 MB freelist. When that follower became leader,
median KV write latency rose from 2.6 ms on the original leader to 23.9 ms.
The application still passed 160/160 workflows at the smaller workload.

With eight routers maintaining 2,048 tenant tables each (245,760 native
subscriptions) and sixteen running placement controllers making catalog writes,
the settled application failed **154/240** workflows. On the prepared leader,
30 additional KV writes measured 193 ms median, 2,325 ms p90, and 3,055 ms
maximum. No routing or placement rollout was in progress during that grade.
After switching all eight routers from native streaming to Consul blocking
queries while retaining the controllers and the prepared leader, the grade
passed **240/240**; write latency fell to 45 ms median and 53 ms p90. The
blocking-query client now shares one watch per service across tenant tables so
the fallback can populate every table without exhausting its own thread pool.

These measurements show a native read/write interaction and a leader-sensitive
storage effect, but a later dedicated run with ample memory passed even at
eight routers. The earlier failure was not reproducible in isolation and must
not be used as a seeded benchmark result. The default rollout `inject` command
and first Codex trial are therefore **not** a validated long-horizon task.

## Bounded-storage latent-leader calibration

`native-latent-6` used one prepared follower, four routers with 512 tenant
tables each, fourteen placement controllers, and background player traffic.
The clean leader passed three 240/240 baseline windows, including one with
background traffic and a one-second player-workflow target. Under an unbounded
laboratory disk, moving leadership to the prepared follower also passed two
240/240 windows. The native free-page layout alone was not a sufficient fault.

The lab then applied an identical **10 MiB/s block-write bound** to all three
Consul cgroups. On the prepared leader the next grade failed **0/240** while
quorum, all application allocations, full admission, and durable-data checks
still passed. Electing an unprepared leader under the same bound recovered
**240/240**, including the latency target. This isolates a leader-sensitive
storage cost rather than a generic throughput-cap failure. After briefly
lifting the bound and quiescing placement writers to let the prepared follower
catch up and win a native election, the original bound and workload were
restored. Two settled grades again failed **0/240** with all safety and
capacity checks intact. The prepared file still held **844,846 free pages**
after the earlier high-volume calibration, compared with **847,681** directly
after preparation. The cgroup bound is a scale normalization for this CloudLab
disk, not a historical Roblox hardware claim.

This result is a manually calibrated incident. The reusable `latent-leader`
runner now encodes the same steps and requires two failed pre-agent grades, but
a fresh automated lifecycle and repeated agent evaluations are still pending.
The current native platform also lacks the historical cache-redeployment,
stale-scheduler, and gradual player-admission recovery phases. The postmortem
task should not yet be called a 73-hour or ultra-long-horizon replica.

## Expanded application

`native-expanded` ran 64 active Nomad allocations, representing 16 service types,
on six Docker-in-Docker workers. It used three upstream Consul 1.10.4 voters,
native Nomad 1.9.7 and Vault 1.18.5, two PostgreSQL shards, four Redis cache pools,
a Redis processing stream, and 10,000 persistent player records.

| Check | Foreground workflows | Expected / observed result |
| --- | --- | --- |
| Healthy baseline | 160 / 160 | Pass |
| Integration baseline | 120 / 120 | Pass |
| Receipt consumers stopped through Nomad | 120 / 120 | Fail: committed work remained unprocessed |
| Consumers restored; backlog processed | 160 / 160 | Pass |
| One-coin database corruption | 80 / 80 | Fail: balance conservation |
| Corruption repaired | 120 / 120 | Pass |

The integration also repeated a request ID and verified exactly one charge.
All final successful checks preserved player identities, acknowledged purchases,
session consistency, and unique transactions. The durable-work check includes
committed purchases whose caller did not receive a successful response.

Implementation validation caught and fixed two application bugs: retries were
blocked by Consul's lock-delay after an already-persisted session, and the route
client retained retired endpoints because native deregistration events omit node
addresses. It now checks existing durable sessions and keys routes by node name.
These were baseline bugs, not intentional benchmark faults.

## Native subscription-load experiment

On the expanded system, increasing each of four routers from 64 to 1,024 tenants
created 61,440 native subscriptions across fifteen downstream service types.
Increasing placement update frequency generated real catalog/Raft writes.
Routing connections were distributed across the three voters.

- Independent grading observed **133/160 successful workflows**: an availability
  failure, with a valid grade and preserved data invariants.
- All six workers and all three voting Consul servers remained ready.
- Consul cgroup memory-event counters (`max`, `oom`, `oom_kill`) were zero on all
  three voters with the 8 GB limits. This experiment did not hit those limits.
- Native CPU profiles were captured from all three voters. No profile content
  was generated from a fault flag or injected diagnostic label.
- Reducing routing tenants back to 64, **while leaving placement write pressure
  high**, restored **160/160 successful workflows** and all grading predicates.

This demonstrates workload-dependent degradation and recovery in the native
system. It does not isolate every internal contributor or establish equivalence
to Roblox's historical channel-contention mechanism. An earlier development run
also failed under load, but hit a 2 GB memory limit; it is explicitly excluded
as clean evidence for the historical mechanism.

## Native storage experiment

On a development follower, offline transactions allocated 512 MiB of values and
deleted alternating records. The resulting native Raft file was 876,834,816 bytes,
with 105,212 free pages, 1,756 pending pages, and 855,768 bytes of freelist data.
Preparation took 5.17 seconds of actual work. The voter subsequently rejoined;
the recovered development application passed 30/30 new workflows and integrity
checks. No artificial maintenance delay or free-page counter was used.

This proves persistent page-layout manipulation and continued Raft operation,
**not** a validated historical-equivalent slow-leader failure. Native latency
calibration, larger layouts, and independent recovery ablations remain necessary.

A separate clean-start run (`native-clean`) passed 30/30 baseline workflows.
After preparing a 128 MiB layout on a follower, the actual upstream `bbolt compact`
operation reduced its Raft file from **219,873,280 to 109,293,568 bytes**. Native
file checking returned `OK`. The voter rejoined and a further **30/30 workflows**
passed with all integrity predicates. This is a small real I/O check, not a claim
that file maintenance at this size is itself a long-horizon task.

## Automated and lifecycle checks

- 35 fast tests passed: seven new durable-state tests and 28 retained Tier 0 tests.
- Three native protocol checks passed inside the application image: deregistration
  without an IP, unhealthy endpoint withdrawal, and snapshot reset.
- Ruff and Python compilation checks passed.
- Worker image compatibility, cgroup delegation, Vault active-state readiness,
  zombie reaping, and unprivileged toolbox credential provisioning were corrected
  during clean-start validation.
- A separate process-lifecycle check verified that an exited daemon is reaped
  while its operational host remains available for repair.

## Evidence and limits

Artifacts are local and ignored by Git:

- `results/roblox-platform/native-expanded/integration-validation.json`
- `results/roblox-platform/native-expanded/native-stress-grade.json`
- `results/roblox-platform/native-expanded/native-pressure-recovery.json`
- `results/roblox-platform/native-expanded/stress-memory-events.json`
- `results/roblox-platform/native-expanded/profiles/`
- `results/roblox-platform/native-dev4/consul-3-storage-preparation.json`
- `results/roblox-platform/native-clean/baseline.json`
- `results/roblox-platform/native-clean/native-compaction-validation.json`

The expanded stack was developed through ordinary rolling deployments; this is
not a frozen evaluation campaign. Logs and manifests preserve that history.
It remains running and recovered as `native-expanded`, with 64 active application
allocations and six ready workers. Its final check passed 120/120 workflows with
no unpublished outbox work. Temporary development and clean-start stacks were
exported and removed; the pre-existing Tier 0 demonstration was preserved.
The new operator surface contains no Tier 0 diagnosis profiles or one-call repair
operations. However, service-graph depth is still fixed across tiers, the fleet
tier has not been capacity-tested, historical-reconstruction mode is not yet
implemented, and no multi-hour agent evaluation has established task difficulty.

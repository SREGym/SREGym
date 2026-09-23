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
runner now encodes the same steps and requires two failed pre-agent grades.
The fresh automated lifecycle is validated below; repeated agent evaluation
remains limited.
The manually calibrated run used a task-labeled extra Bolt bucket; that gave
the first agent an unnatural clue. The revised fixture uses temporary entries
in the native Raft log bucket and deletes them before restart, so its own
storage statistics and outcome need fresh validation.
The current native platform also lacks the historical cache-redeployment,
stale-scheduler, and gradual player-admission recovery phases. The postmortem
task should not yet be called a 73-hour or ultra-long-horizon replica.

`native-latent-7` is a fresh run of the revised fixture. It allocates and
deletes temporary entries inside the existing Raft `logs` bucket; the resulting
file is **6.47 GB**, with **1,571,480 free pages** and a **12.6 MB freelist**.
No task-named bucket remains. This fixture required a **20 MiB/s** common
block-write bound for a stable clean-leader baseline on this node: under the
earlier 10 MiB/s bound all 240 workflows returned successfully, but too many
exceeded the one-second target. At 20 MiB/s, the clean leader passed **240/240**
with background player traffic. The automated injector then elected the
prepared follower without changing a Nomad job, restored all 14 placement
controllers and the common bound, and recorded two consecutive **0/240**
pre-agent grades. Quorum, capacity and durable-data checks still passed.
The preparation and fault are validated. A subsequent fresh run,
`native-latent-8`, completed the full automated `up` path with the new
20 MiB/s default: two settled clean baselines passed 240/240, as did an
additional 240/240 window with background traffic. The injector elected the
prepared follower on its first native election without changing Nomad jobs.
Two independent post-injection grades both failed **0/240**, while all
non-workflow checks stayed green. A third Codex trial then ran on this incident.

### First latent-incident Codex trial

Codex CLI 0.155.1 with `gpt-6-astra` ran on the manually calibrated
`native-latent-6` incident. The disposable benchmark took **14m42s** and exited
normally. The agent diagnosed streaming pressure and the large Raft store,
switched routing to shared blocking queries, archived the bloated replica's
Raft directory and rebuilt it from the healthy quorum. It verified 664 of its
own player workflows across all 512 tenants over five minutes, with no errors
and a reported maximum latency of 446 ms.

The independent post-agent grade passed **240/240** player workflows and all
latency, data-integrity, backlog, quorum, and full-admission checks. The total
grade **failed** because the agent left the placement job at **2/14 required
replicas** after reducing Consul write pressure. Its incident report declared
recovery, but the fixed capacity check detected a changed deployment the
agent's verification did not address. This is one trial on the older, task-labeled Bolt
fixture; it is not a pass-rate estimate for the revised scenario. The trace,
report, host logs, and independent `benchmark.json` are under
`results/roblox-platform/native-latent-6/` on this node.

### Second latent-incident Codex trial

`native-latent-7` used the revised bucket-free Raft fixture and a fresh,
validated incident. Codex CLI 0.155.1 with `gpt-6-astra` exited normally after
**10m10s**. It diagnosed slow Consul writes, switched the routers to blocking
queries, reduced placement from 14 controllers to 2 and reconciliation from
0.05 to 30 seconds, and compacted the affected Raft file. Its own four-minute
verification reported **1,200/1,200 workflows**, p95 216 ms, maximum 314 ms,
and declared the incident resolved.

The independent post-agent grade passed **240/240** player workflows, the
one-second latency target, data integrity, backlog, quorum, full admission and
worker readiness. The total grade **failed** because the placement fleet was
still **2/14**. This again detected a reduced deployment despite
the agent's green player probes. The agent was given one hour and did not time
out. Its trace, final report and evaluator result are preserved under
`results/roblox-platform/native-latent-7/` on this node.

### Third latent-incident Codex trial

`native-latent-8` used the same revised fixture but completed the entire
automated clean-start and injection path. Codex CLI 0.155.1 with `gpt-6-astra`
exited normally after **9m54s**. It switched the routers to shared blocking
queries, reduced placement to **1/14**, compacted the affected Raft file, and
verified a three-minute sample across routing cohorts. The independent grade
passed **240/240** player workflows and all integrity, backlog, quorum,
admission, worker-readiness and latency checks; it failed the total grade on
the then-current placement replica-count check.

The three trials therefore provide **0/3 full passes and 3/3 player-workflow
recoveries**, under a one-hour limit. They do not establish ultra-long-horizon
difficulty: diagnosis and mitigation still took roughly ten to fifteen minutes.
Moreover, the original placement workers duplicated catalog updates, so the
fixed 14-replica requirement was too prescriptive to justify these failures as
real service outages. The next revision makes each placement worker responsible
for a distinct player-reservation shard and grades live shard behavior. The
three results above are preliminary results for the earlier task version.

### Outcome-based placement validation

The revised application assigns each of the 14 placement allocations a stable
Nomad index and a distinct player-reservation shard. Allocations publish their
live endpoint through Consul; the allocator verifies ownership and calls the
responsible allocation before creating a player session. The grader probes all
shard endpoints, so it can accept any repair that restores the service behavior
without prescribing a Nomad job count.

In `native-shards-1`, the fresh sharded stack passed **240/240** under the
20 MiB/s bound. A placement scale-down initially also disturbed the streaming
routers; switching those routers to their supported blocking-query mode
restored a **240/240** control grade. With routing held fixed, reducing
placement from 14 to one made **32/240** workflows succeed and the live-shard
check fail, while Consul quorum and the other service-capacity checks remained
green. Restoring all 14 allocations and live shard endpoints brought the same
grade back to **240/240**. This clean/fault/clean control establishes a player
outcome for incomplete placement restoration. A fresh source-blind Codex trial
on this revised task is still pending.

The first sharded traffic run exposed another native client bug: after a
streaming reconnect, a router discarded its previous endpoint table before a
replacement snapshot was complete, creating intermittent 503s on an otherwise
healthy leader. The streaming client now swaps tables only at Consul's
`EndOfSnapshot` event. Placement is also split into 14 reservation shards and
seven catalog writers, so functional placement capacity does not require 14
duplicate catalog-update loops. With four routers at **128 tenants each** and
the same 20 MiB/s common disk bound, a manually calibrated run passed three
consecutive **240/240** background-traffic grades. Electing the prepared leader
then failed two valid grades at **0/240**, with quorum, live placement shards,
 admission and durable-data checks intact. This control included live Nomad
rollouts during calibration; a fresh automated run using the final source and
configuration followed. `native-shards-3` completed `up` with two settled clean
baselines, then passed two more **240/240** grades under background traffic.
Its injector elected the prepared follower on the first attempt without
changing a job. Both valid pre-agent grades failed **0/240** while live
reservation shards, quorum, admission and durable-data checks remained green.
A source-blind Codex trial on that fresh incident followed.

### First corrected-task Codex trial

Codex CLI 0.155.1 with `gpt-6-astra` ran on `native-shards-3` for **14m34s**
and exited normally. It reduced streaming subscription and catalog-write load,
compacted the prepared Raft database, and retained all 14 placement partitions.
Its five-minute check reported **652 successful workflows**, a maximum latency
of 279 ms, and no new edge errors. The independent post-agent grade **passed**:
**240/240** workflows under the one-second target, all 14 live reservation
shards, data integrity, acknowledged work, backlog, quorum, admission, worker
readiness and service capacity. This is **1/1** on the corrected task so far;
further fresh trials are needed to estimate repeatability.

The second corrected run, `native-shards-4`, used the same one-hour limit and
fresh clean/fault controls. Codex exited normally after **13m50s**. It reduced
streaming and catalog churn, compacted the affected Raft file, kept all 14
placement shards, and checked 1,024 player workflows plus retries across all
cohorts. Its report recorded a 315 ms maximum latency and zero errors during
187 seconds of full-admission observation. The independent post-agent grade
also **passed 240/240** workflows, all live shards and every latency, safety,
quorum, admission and backlog check. This brought the corrected task to
**2/2 passes** before the third fresh run.

The third corrected run, `native-shards-5`, passed two clean 240/240 grades
under background traffic. The prepared follower won after three ordinary
elections; two valid pre-agent grades then failed **0/240**, with the other
checks green. Codex exited normally after **11m41s**. It reduced streaming and
catalog-update load, compacted the affected Raft file, and retained every
placement shard. Its five-minute verification reported 3,046 live requests
without errors and 768 successful workflow checks, with a 260 ms maximum.
The independent post-agent grade **passed 240/240** workflows and every
latency, placement, integrity, backlog, quorum, admission and capacity check.

Across the **three fresh corrected-task runs**, Codex passed **3/3** independent
grades in 14m34s, 13m50s and 11m41s, with no timeouts. Each run had two
valid pre-agent 0/240 grades and a 240/240 post-agent grade. This small sample
shows the benchmark is reproducible and solvable; it does **not** show
ultra-long-horizon difficulty. The agents all found the high Consul load,
reduced streaming/catalog pressure, compacted the bloated Raft file, and
verified user and data outcomes within fifteen minutes. The next fidelity step
is to make cache redeployment, stale scheduler state and staged player return
executable, then repeat the agent evaluation. The public postmortem describes
those as consequential recovery phases, not just the initial Consul diagnosis.
The sanitized machine-readable grades, usage and source hashes are preserved in
[benchmarks/corrected-trials.json](benchmarks/corrected-trials.json).

### First cache recovery experiment

`native-recovery-2` scheduled four Redis pools as Nomad allocations backed by
worker storage. The fresh stack passed two 240/240 warmup grades and a 240/240
grade under the common Consul disk bound. The first cache fault did not persist:
Redis kept its append-only files in a writable child directory. During injection
the fault was corrected to remove write access from that directory and its files,
and the replacement allocation then exited with a native Redis error. Two valid
pre-agent grades failed 0/240 while data integrity remained intact. This is an
**exploratory run**, not a cleanly seeded evaluation.

Codex CLI 0.155.1 (`gpt-6-astra`) restored the cache, reduced Consul streaming
and catalog pressure, compacted the affected Raft file, and passed the independent
final grade: **240/240** workflows plus every latency, integrity, placement,
capacity, quorum, admission, and backlog check. The wrapper took **9m42s**. The
cache layer made the environment more faithful but did not increase difficulty.
The sanitized result is in
[benchmarks/recovery-exploratory.json](benchmarks/recovery-exploratory.json).
A subsequent clean revision distributed 24 pools across six workers and faulted
all four pools on one still-ready worker. `native-recovery-3` passed two 240/240
warmup grades and a 240/240 prepared baseline under background traffic. Its
injector elected the prepared Consul follower and obtained two valid 0/240
pre-agent grades; all four replacement Redis allocations exited with code 1.
Codex CLI 0.155.1 (`gpt-6-astra`) restored the caches, reduced streaming and
catalog pressure, compacted the real Raft file, and performed repeated cohort,
queue, and database checks. It exited normally after **18m23s**. The independent
post-agent grade passed **240/240** workflows and every integrity, latency,
capacity, placement, quorum, admission, and backlog check.

After the agent run, a cache-only control changed no Consul settings: stopping
the four repaired pools with their storage unwritable produced **200/240**
successful workflows and a valid failing grade. Restoring the same storage and
allowing Nomad to restart Redis returned the grade to **240/240** with every
check passing. This isolates the cache layer's player impact from the Consul
fault. The sanitized trial and control results are in
[benchmarks/recovery-immediate-trial.json](benchmarks/recovery-immediate-trial.json).
The 18-minute repair still falls far short of the intended multi-hour task.

The next revision arms the cache worker defect while existing pools continue
serving, then uses a Nomad-scheduled cache reconciler to begin rolling pools
only after sustained healthy Consul KV writes. This should expose cache
bootstrap work *after* the first Consul mitigation. The first source-blind
outcome grade below validates that sequence.

### Deferred cache rebootstrap trial

`native-recovery-4` is a fresh staged run. Its first warmup had a transient
220/240 bootstrap result, then two consecutive 240/240 windows passed; the
prepared baseline under the shared Consul write bound also passed 240/240.
After the prepared follower became leader, the cache reconciler measured a
1.9-second real KV write and held its rollout idle. Both valid pre-agent grades
failed 0/240 with all 24 cache services still passing. The private redeployment
request was pending, so the agent encountered the Consul outage first.

During the source-blind Codex run, reducing routing and catalog pressure alone
left Consul writes slow. The agent compacted the affected native Raft file;
the reconciler then observed healthy KV writes, stopped `cache-0`, and its
replacement Redis allocation exited with code 1 on the still-ready worker.
Codex diagnosed the worker storage permissions and restored them. The normal
controller resumed and completed all 24 replacements in **7m21s** from its
first stop to completion. Codex then observed 3.5 minutes of clean full
admission, checked 228/228 further workflows, and audited all 7,706 purchases
and matching receipts. It exited normally after **17m56s**. The independent
post-agent grade passed **240/240** workflows and every integrity, latency,
placement, capacity, quorum, admission, backlog, and cache-generation check.
The sanitized result and rollout timing are in
[benchmarks/recovery-staged-trial.json](benchmarks/recovery-staged-trial.json).

This first staged run predates a small controller epoch guard and a cold-cache
preparation change. Those changes are pushed but require a fresh validation.
The staged failure is causally closer to the postmortem, yet a sub-18-minute
agent repair still does not establish an ultra-long-horizon task.

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

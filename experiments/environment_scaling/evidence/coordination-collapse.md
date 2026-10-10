# Coordination collapse: a long horizon, lying tools, and losses you keep

`coordination_collapse_single` and `coordination_collapse_replicated` exist
because of a measured result: the three families screened before them scored
**0% difficulty**, every attempt solved in roughly a quarter of its budget. See
the [calibration report](difficulty-calibration.md).

Those families varied topology, state size, telemetry volume and grading
contract. None of them used the three levers the SREGym 2.0 proposal actually
lists for long-horizon work, so this family is built on all three, modelled on
the shape of the [Roblox 2021 Consul
outage](https://blog.roblox.com/2022/01/roblox-return-to-service-10-28-10-31-2021/).

## 1. A recovery floor you cannot compress

Recovery is a sequence, and each phase is gated on the previous one having
*settled* for real time:

| Phase | Gate |
|---|---|
| Shed streaming load | none — this is the only unconditional action |
| Compact the store | leader held for the full stability window |
| Rebuild scheduler state | store compacted, leader still stable |
| Caches warm | all of the above, then real elapsed time |
| Admit traffic | a staircase, each step held before the next |

| Setting | `single` | `replicated` |
|---|---:|---:|
| Stability window | 60 s | 90 s |
| Cache warming | 120 s | 180 s |
| Admission step | 45 s | 60 s |
| **Measured recovery floor** | **360 s** | **510 s** |

Measured by driving the state machine directly:

| Strategy | Elapsed | Admitted | Regressions | Dropped | Final capacity |
|---|---:|---:|---:|---:|---:|
| Flawless | 366 s | 1.00 | 0 | 0 | **1.00** |
| Rushed | 187 s | 0.00 | 1 | 300 | 0.10 |
| Never sheds | 300 s | 0.00 | 0 | 0 | **0.00** |

Rushing *finishes sooner* with zero capacity and permanent loss. Never shedding
makes no progress at all, however long it waits.

**Impatience is the expensive mistake.** `compact` requires the leader to have
held for the full window, and an attempt made too early *restarts that window*
— it is a write storm. Polling the endpoint to discover whether the leader is
ready guarantees it never becomes ready. Checking is free, but only through the
read-only truth endpoint, which publishes `leader_stable_seconds` and the
requirement. This is stated in the incident guide the agent is handed; it is a
documented property, not a trap.

Because the floor is 360 s before any diagnosis, the problem declares
`recommended_agent_timeout_seconds = 2700`. Running this at the 900 s the earlier
families used would measure the budget rather than the agent.

## 2. Tooling that lies, not tooling that is missing

The cascade family removed dashboard panels. That is a weaker thing. Here:

- **`/status` serves a pre-incident snapshot** while the leader churns. It
  reports the cluster healthy with 12 watch subscriptions; the real number is 96
  and nothing is serving. An agent that trusts it sees no incident at all. It
  starts telling the truth once the cluster is genuinely stable.
- **`/keys` is the amplification source.** The obvious way to ask how big the
  store is hangs for 60 s *and adds compaction debt*. Using the diagnostic makes
  the incident worse.
- **The aggregated metrics collector is circularly dependent.**
  `discovery-metrics` resolves its scrape targets through the coordination
  service it monitors, so while coordination is degraded it returns no series at
  all — it says so, and names the way around itself. Each service's own
  `/metrics` is honest and directly reachable.

No liveness probe in this family doubles as a readiness probe: a coordinator
serving nothing still answers `/health`, so a green probe is never evidence.

## 3. Cost that accumulates, damage that does not heal

- **Dropped requests are permanent.** Admitting more than the cluster can serve
  drops requests into a counter no action decreases. Admission is pulled to zero
  when the collapse develops, as an edge does when its backends stop resolving,
  so the meter ticks only on an agent's own premature admissions — the cost is
  attributable to choices, not to how long the agent took. The grader has a budget
  (`20 × floor`, so 7,200 for `single`); the reference recovery loses **zero**,
  so the budget is headroom for one recoverable mistake, not a licence.
- **A large admission while caches are cold re-collapses the cluster** and costs
  the whole warming phase again. The original incident's restart storm, in one
  rule.
- **`force-reset` is a one-way door.** It wipes a member's store; that member can
  never serve again. Reaching full service on a cluster with no redundancy left
  is graded `cluster_redundancy_destroyed` — it is not a recovery. Destroying a
  majority is `quorum_permanently_lost`, reported ahead of everything else,
  and reference recovery refuses to pretend it can clean that up.

## What counts as recovery

Graded on the end state, what it cost to get there, and what survived:

- the leader healthy and holding, store compacted, scheduler state fresh;
- `serve_capacity_fraction` and `admitted_fraction` both 1.0;
- customer requests served with none failing or shed;
- **held across a window longer than any single settling time**, so a recovery
  caught mid-warming fails as `recovery_not_durable` rather than passing on
  timing;
- dropped requests within budget, and no member destroyed;
- plus the shared `SaaSOracle` guarantees, so an agent cannot "recover" by
  discarding business state.

## What this does and does not reproduce

The cluster **deploys healthy and serving**, and injection drives it into the
collapse. That is not cosmetic: the campaign gate requires the grader to pass
before injection, so an application that deployed already broken could never be
screened, and a degraded baseline cannot show the incident caused anything.

**Real:** the degradation is computed from the service's own state — watch
subscriptions, key count, compaction debt — not toggled by a flag. The phase
gates, the regression on premature action, the permanent loss counter and the
irreversible member destruction are all mechanical. State lives on a persistent
volume, so a restart genuinely cannot clear it, and the validator proves that.

**Not real:** this is not Consul and there is no BoltDB. It is a purpose-built
service that reproduces the *shape* of streaming contention and pathological
store growth, with one coordinator process standing in for a three-member
cluster whose membership is tracked in state. The clock is compressed: minutes
stand in for the original's 73 hours, and the ratio between phases is a design
choice rather than a measurement of Roblox's. Nothing here models their DNS
infrastructure or their actual traffic shape.

**Not modelled:** the hypothesis-testing dead ends of the real investigation,
and the multi-day human coordination. The task is hard because its recovery is
long and unforgiving, not because it has a hidden root cause to guess.

## Run admission

```sh
PYTHONPATH=/opt/sregym python tests/integration/validate_coordination_collapse.py \
  --tier single --output results/coordination-admission.json
```

It requires the collapse to be real and observable, then proves each lever:
`/status` disagreeing with the truth endpoint, a restart changing nothing, three
compaction probes each restarting the window, patience then unlocking the
sequence, a rushed admission regressing with permanent loss, the full reference
sequence taking at least the floor and passing, and a `force-reset` failing even
with service restored.

The three-attempt comparison, once admission passes:

```sh
python scripts/evaluate_deathstarbench.py \
  --applications mattermost --incident coordination_collapse \
  --tiers single --agent claudecode --model claude-opus-5 \
  --attempts 3 --profile svelte --agent-timeout 2700 \
  --output results/coordination-screen
```

## Codex screen: 3 of 3 solved, and the design did not bite

`gpt-6-astra`, CLI 0.160.0, `svelte`, mitigation only, 2,700-second budget, with
no authored briefing — the agent discovered the operator API from the service's
own route listing. Lifecycle gate 8/8.

| Attempt | Verdict | Agent time | Dropped | Regressions | Elections | Members |
|---:|---|---:|---:|---:|---:|---:|
| 1 | solved | 437 s | 0 | 0 | 1 | 3 |
| 2 | solved | 374 s | 0 | 0 | 1 | 3 |
| 3 | solved | 669 s | 0 | 0 | 1 | 3 |

Against a 360-second floor, two of three land within 80 seconds of the
theoretical minimum. **Every protective mechanic went untouched**: no rushed
gate, no `compact` poll restarting its own stability window, no cold
over-admission, no `force-reset`. The agent read the refusal messages and waited.

That is a negative result for the premise of this family, and worth stating
plainly. A recovery floor makes a task take time; it does not make a capable
agent fail. The levers this family was built on — long horizon, lying tools,
accumulating irreversible cost — produced a slower task, not a harder one. The
family that does fail a frontier agent is
[regional failover](gitlab-regional-failover.md), which asks for a judgement
between two acknowledged write sets rather than a procedure.

Admission remains a correctness gate rather than a difficulty result; see the
[calibration report](difficulty-calibration.md) for the cohort rules.

## Completed live admission

The `single` tier passed full admission in 883 s with no errors and clean
cleanup. Every lever was proved on a live cluster rather than asserted.

| Elapsed | Stage | Verdict |
|---:|---|---|
| 58 s | collapse observable | `coordination_leader_unstable` |
| 252 s | after a rushed admission | `coordination_not_serving` |
| 743 s | reference recovery | pass |
| 840 s | held, graded again | pass |
| 842 s | after a `force-reset` | `cluster_redundancy_destroyed` |

**The collapse is real.** Write latency 534 ms against a 400 ms budget, 96 watch
subscriptions, serve capacity 0.0 — the same numbers the unit tests compute, so
the physics transfer unchanged to a cluster.

**The tools lie as designed.** `/status` reported **12** watch subscriptions
while the truth endpoint reported **96**. An agent trusting the status endpoint
sees no incident.

**The aggregated collector was blind**, confirming the circular dependency.

**A rollout restart changed nothing** — latency and subscriptions identical
afterwards, because the degradation is persisted data.

**Impatience cost the window.** Three `compact` probes during the stability
window each returned "this attempt restarted the stability window", and the store
stayed uncompacted. Waiting the window out without touching it then unlocked
both `compact` and `rebuild-scheduler` immediately.

**Rushing admission regressed the recovery.** The ledger recorded
`admitted 100% with caches 1% warm`: one regression, scheduler state wiped,
warming reset to zero, and **300 requests dropped permanently**.

**The reference recovery took 393 s**, above the 360 s floor, and passed with
capacity and admission both at 1.0. The 300 dropped requests from the earlier
deliberate rush were still counted — the meter never resets — and sat inside the
7,200 budget, which is what that headroom is for. A second grade 97 s later
returned identical numbers, so the recovery is steady rather than oscillating.

**Destroying one member failed the recovery** one second after it had passed,
with all three members still counted as available for quorum but redundancy
gone.

The ledger captured the whole sequence for an operator to read back:
`incident_started`, `load_shed`, `compaction_too_early`, `leader_elected`,
`compacted`, `scheduler_state_rebuilt`, `cache_warming_started`,
`admission_changed`, `regression`, `member_destroyed`.

# GitLab regional failover: two acknowledged histories

`gitlab_regional_failover_single` and `gitlab_regional_failover_replicated` are
modelled on the shape of the [GitHub 2018 regional database
failover](https://github.blog/2018-10-30-oct21-post-incident-analysis/). They are
the first family in this workstream where **no single restore recovers the
incident**, which is why they were added: the preceding candidates were all
passing their three-attempt screens.

## The incident

A brief partition between two database regions makes the orchestrator promote a
lagging replica. Three write sets result:

| Set | Where it lives after the failover | Public identity |
|---|---|---|
| Shared history | Both the live database and the demoted snapshot | Stable |
| Writes acknowledged during the partition | The demoted primary's snapshot only | Reissued to other issues |
| Writes accepted after promotion | The live database only | Held, and live |

Both tails were acknowledged to clients. Because each region kept allocating
issue IIDs after they diverged, the same `(project, IID)` was issued twice in the
same project for different issues — so some customers' issue URLs now resolve to
somebody else's issue.

| Dimension | `single` | `replicated` |
|---|---:|---:|
| Tenant projects | 3 | 5 |
| Shared-history issues | 12 | 60 |
| Orphaned acknowledged writes | 6 | 30 |
| Writes accepted after promotion | 6 | 30 |
| PostgreSQL members | 1 | 3 |
| Persistent volumes | 5 | 7 |

## Why both obvious actions fail

The grader is outcome-based and separates the two failure modes by reason code:

- **Failing back** — restoring the demoted primary's snapshot over the live
  database recovers the orphans and destroys every write accepted after
  promotion. Graded `post_promotion_writes_lost`.
- **Doing nothing** — restoring availability without reconciling abandons the
  writes the demoted primary acknowledged. Graded `orphaned_writes_abandoned`.
- **Reusing the colliding IID** — reinserting an orphan at its original identity
  overwrites a live post-promotion issue. Also `post_promotion_writes_lost`.
- **Reconciling twice** — `acknowledged_writes_duplicated`.

Only retaining both sets passes. The contract the grader applies is stated in
`/recovery/failover.txt` rather than left to be guessed: post-promotion issues
keep the identity they already have, and each orphaned write keeps its project
and content while necessarily receiving a new IID.

Recovery is graded on the resulting system state, not a command sequence:

- every orphaned write present exactly once, matched on project and content;
- every post-promotion issue still at its own IID with its own content;
- the shared history untouched;
- both evidence snapshots unmodified (`recovery_evidence_changed`);
- Git content unchanged, and fresh writes working afterwards;
- the configured PostgreSQL membership and synchronous durability restored, with
  replicas converged — inherited from the shared `SaaSOracle`.

Additional ordinary writes are allowed: validation probes and new work add rows,
and that is not a failure.

## Evidence the agent gets

`/recovery/east/pre-partition.dump` (the demoted primary's full state) and
`/recovery/east/last-replicated.dump` (the shared history) are read-only
evidence. `/recovery/journal/partition-acknowledged.json` lists the orphaned
writes in acceptance order. `/recovery/orchestrator.log` records the failover
decision, and `/recovery/chat.txt` carries the on-call signal — including a DBA
explicitly warning against the fail-back, and support reporting the URL
collisions. The console has PostgreSQL tools for inspecting a dump in a scratch
database.

## What this does and does not reproduce

The divergence is real: two genuinely different histories, both containing writes
acknowledged through the real GitLab API, with real colliding public identities,
and a grader that accepts neither alone. The mechanism is not. There is one
CloudNativePG cluster, and promotion is modelled by restoring the last replicated
snapshot rather than by partitioning a network and inducing split-brain in two
independent regions. Timings are not GitHub's, and both regions share one host.

Not implemented: webhook/backlog draining and replica rebuild as agent work. The
[notification families](gitlab-notification-recovery.md) already cover queue
drainage, and repeating it here would add runtime without adding a new decision.
GitHub's 24-hour degraded tail is therefore not represented.

## Run admission

Negative controls are the gate. Run in an idle DinD cluster:

```sh
PYTHONPATH=/opt/sregym python tests/integration/validate_gitlab_failover.py \
  --tier replicated --output results/failover-admission.json
```

It requires healthy operation, then `orphaned_writes_abandoned` after the
failover, then `post_promotion_writes_lost` for a fail-back, then the same
abandonment verdict once the promoted history is rebuilt, then a partial
reconciliation still failing, then a full reconciliation passing, then
`acknowledged_writes_duplicated` for a repeat, and finally a rebuilt recovery
that survives an application restart with both evidence snapshots unchanged.

The three-attempt comparison, once admission passes:

```sh
python scripts/evaluate_deathstarbench.py \
  --applications gitlab_ce --incident regional_failover \
  --tiers single replicated --agent claudecode --model claude-opus-4-8 \
  --attempts 3 --profile svelte --agent-timeout 900 \
  --output results/failover-comparison
```

Admission is not a difficulty result, and this family has **not** yet been
screened against any agent. See the [calibration report](difficulty-calibration.md)
for the cohort rules that apply.

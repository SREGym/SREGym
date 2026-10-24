# Regional database failover with real MySQL replication (#1069)

Status: plan, nothing built yet. Anything marked **(verify)** is an assumption I have not tested.

## What this is

A problem based on GitHub's October 2018 outage ([post-incident analysis](https://github.blog/news-insights/company-news/oct21-post-incident-analysis/)).

A 43 second network partition made Orchestrator promote a West Coast MySQL replica to primary. The East primary had acknowledged writes that never replicated, so after the network came back both sides held writes the other lacked. Webhook and Pages work piled up behind the outage. Recovery meant keeping both histories, rebuilding replication, and draining the backlog without dropping or duplicating anything.

## What already exists

`gitea_regional_failover` and `gitlab_regional_failover` (from #1062) cover the same story, but they fake it. Promotion is a `pg_restore` of a dump taken earlier. There is no live replication, no partition, no lag, and no backlog. The GitLab one was solved 10/10 in the screen, so it is unregistered as saturated.

This problem keeps the Gitea app, the workload client, the receipt-based oracle pattern and the tier scheme, and replaces the fake with the real mechanism.

Reused from `main`:

- `sregym/service/apps/gitea/` (app, workload client, `snapshot()`, fixtures)
- `sregym/conductor/problems/apps/gitea/regional_failover.py` (structure of the problem)
- `sregym/conductor/oracles/apps/gitea/divergent_recovery.py` (receipt checks)
- `PostgresApplication` / `components/cnpg.py` as the pattern for a database component
- the isolated verifier already on `main`
- Calico, which the Kind setup already installs

## What is new

1. A MySQL primary and replica with async GTID replication, as a component beside `cnpg.py`. I plan to use a plain StatefulSet and the official `mysql` image rather than an operator, so there is nothing extra to install **(verify the image and replication setup)**.
2. Gitea running on that MySQL (`GITEA__database__DB_TYPE=mysql`). Today it only runs on CloudNativePG Postgres **(verify the Zoo import fixture loads on MySQL)**.
3. A small failover manager. It probes the primary from the other region, and after repeated failed probes promotes the replica and repoints the write service. This plays the part of Orchestrator.
4. A real partition using a Calico NetworkPolicy between the two regions. No Chaos Mesh.
5. Real divergence: acknowledged writes on both sides.
6. A webhook backlog left undelivered at handoff.

## Layout

One Kind cluster, two namespaces standing in for two regions. They share a control plane, so this models a database failover, not a control-plane outage.

```
region-east:  MySQL primary  <--- async GTID replication --->  region-west: MySQL replica
              Gitea server (writes go through the gitea-db-rw service)
              failover manager (probes from the west side)
              webhook receiver (counts deliveries)
```

## Fault lifecycle

1. Deploy, seed, and check the healthy baseline: reads and writes work, the replica has caught up (compare GTID sets).
2. Apply the NetworkPolicy that blocks east and west from each other.
3. Write through Gitea while partitioned. These writes are acknowledged on the east primary and never reach the replica. The workload client records a receipt for each.
4. The failover manager sees failed probes, promotes the west replica, and repoints `gitea-db-rw`.
5. Write more through Gitea. These land on the new primary and reuse issue numbers the east side already handed out, like the existing problem does.
6. Remove the NetworkPolicy. Keep the old primary from silently taking writes again **(open question: how exactly it is demoted, see below)**.
7. Webhook deliveries that were due during the outage are still undelivered at handoff.
8. Hand over to the agent.

Receipts live in the conductor, outside the cluster, so the agent cannot read or edit them.

## What the agent has to do

- Find the orphaned writes in the old primary (they are the GTIDs the new primary lacks) and re-apply them to the surviving history without clobbering the post-promotion writes.
- Fence the old primary and rebuild it as a replica of the new one.
- Get replication healthy again with no errant GTIDs.
- Let the webhook backlog drain, each event delivered exactly once.

## Grading

The oracle reuses the receipt checks and adds:

- every receipt from both histories is present exactly once, with matching content
- one writable primary, the other member is a replica, replication threads running, GTID sets converged
- reads agree across members
- Gitea still serves under continuing workload
- Git repositories intact
- each accepted webhook event reached the receiver once (no drops, no duplicates)

Shortcuts the oracle has to reject: resetting to an empty database, dropping the orphaned side, deleting the queue, routing around the intended service, returning fake healthy responses.

## Reference repair

A script that does the intended recovery, run through the same interface the agent has. It must not read the conductor's receipts or any grader state.

## Scale

Tiers follow `single` / `replicated`. A tier sets the number of shared records, orphaned writes, post-promotion writes, webhook events, and replicas per region. The causal chain stays the same at every size. I start with one small tier that fits the standard Kind cluster (1 control plane, 3 workers). Larger tiers come after the small one works. Development on CloudLab.

## Order of work (one PR, separate commits)

1. Spike: MySQL primary/replica in Kind. Show replication, a partition, a promotion, and an errant transaction. If this does not work, nothing else matters.
2. Gitea on MySQL, healthy and taking writes.
3. Failover manager and Calico partition.
4. The problem, receipts, and reference repair.
5. Oracle checks.
6. Tiers, registry entry, docs.
7. Validation: healthy baseline, no-op fails, reference repair passes, unsafe actions fail, clean teardown.
8. Five baseline agent runs, recording model, harness, budget and seeds. Target 0/5. If more than 2/5 pass, make the causal chain harder and rerun on fresh seeds. Report infrastructure failures separately.

## Open questions

- How the old primary is demoted after the partition heals. The 2018 East side stayed up with its writes. Options: the manager sets it read-only, or it is left running and the agent has to notice. The first is closer to the existing problem; the second is harder and closer to the incident.
- How Gitea redelivers webhooks after the database is repaired, and whether I can leave a backlog in a way that does not depend on Gitea internals. Needs a prototype.
- Whether the probe window and partition length can be kept short enough for CI while still triggering promotion.
- Whether tiers should add database groups or only more data.

## Not in this PR

GitLab, the GitLab database-deletion problem (#1074), CodeHub, and any new application.

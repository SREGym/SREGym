# Regional database failover with real MySQL replication (#1069)

Status: built and registered as `gitea_mysql_regional_failover_small` and `gitea_mysql_regional_failover_large`.

## What this is

A problem based on GitHub's October 2018 outage ([post-incident analysis](https://github.blog/news-insights/company-news/oct21-post-incident-analysis/)).

A 43 second network partition made Orchestrator promote a West Coast MySQL replica to primary. The East primary had acknowledged writes that never replicated, so after the network came back both sides held writes the other lacked. Webhook work piled up behind the outage. Recovery meant keeping both histories, rebuilding replication, and delivering the backlog without dropping or duplicating anything.

## What already exists

`gitea_regional_failover` and `gitlab_regional_failover` (from #1062) cover the same story, but they fake it. Promotion is a `pg_restore` of a dump taken earlier. There is no live replication, no partition, no lag, and no backlog. The GitLab one was solved 10/10 in the screen, so it is unregistered as saturated.

This problem keeps the Gitea app, the workload client and the divergence checks from that work, and replaces the fake with the real mechanism.

## Layout

One namespace, `gitea`. Regions are a `region` pod label, and each region's pods are pinned to its own Kind worker (`topology.kubernetes.io/region=east|west`), so traffic between regions crosses a link. The partition is a Calico policy that denies traffic between the two labels. No Chaos Mesh.

```
region=east node:  MySQL gitea-db-east  --- async GTID replication --->  region=west node: MySQL gitea-db-west
                   Gitea (writes go through gitea-db-rw)                    webhook receiver (hidden)
                   webhook relay  ------------------------------------->    Orchestrator
                   application client, live users (hidden)
```

- `components/mysql.py`: one StatefulSet per region on the official `mysql:8.4.6` image, the `gitea-db-rw` service, replication, the partition, and checks.
- Gitea runs on MySQL through `GITEA__database__DB_TYPE=mysql`.
- **Orchestrator** (`orchestrator.py`, visible) watches both servers from the west node, as GitHub's did. It keeps `gitea-db-rw` pointed at the master it records, the way GitHub's load balancers followed Orchestrator through Consul. It recovers a dead master after three failed checks, and is blocked for an hour after a recovery (`RecoveryPeriodBlockSeconds`). Its API follows the real one: topology, analysis, audit, global recovery switches, downtime, candidates, relocate, replica start/stop/reset, read-only switches, `graceful-master-takeover[-auto]`, and `force-master-failover`/`force-master-takeover`. Its state lives in the hidden namespace, as its own backend database would.
- **Cross-region latency**: hidden privileged pods put a 10 ms netem delay on each worker's traffic to the other region's node and pod CIDRs. Gitea creates an issue in about 0.5 s beside its database and about 3.7 s across the link, because it makes about 170 round trips per issue. A delay closer to GitHub's 60-70 ms round trip makes each write take longer than the outage budget.
- Gitea sends its webhooks to a relay beside it, standing in for GitHub's job queue. The relay forwards them in order, can be paused, resumed and have items dropped, and lists the raw queued deliveries (headers and payload) with no summary.
- The webhook receiver checks Gitea's HMAC signature and records each delivery with the issue id, number and comment id.
- A live users pod opens an issue about every 2 seconds through Gitea, as a non-admin member with write access to one repository, and records how long each took.
- The receiver, live users, latency pods and Orchestrator's state run in the hidden `khaos` namespace, so the agent cannot read their code, logs or records.
- Setup and grading run their Gitea and MySQL checks from two idle client pods in `khaos`. MySQL is queried by pod IP, so a re-pointed service cannot answer for a server.
- The agent's kubectl proxy refuses node proxying, minting service account tokens, and privileged, hostNetwork and hostPath pods. It also blanks Calico IPAM entries that name pods in hidden namespaces.

## Fault lifecycle

1. Deploy, seed, add webhooks, labels and milestones, and start Orchestrator watching east. Write shared issues; wait until west has every GTID and every webhook was delivered.
2. Apply the partition between the regions. Orchestrator still sees both servers.
3. Gitea keeps writing to east, about 950 writes in the small tier:
   - issues, comments, closes, labels, assignees, milestones and reactions on new and shared issues;
   - Git pushes, so branch records drift from Git;
   - a new repository with files, issues and comments.

   Their events queue in the relay, which cannot reach the receiver.
4. Pause the relay, as GitHub paused webhook delivery. Cut east off from Orchestrator too: it declares east dead, promotes west across regions and routes writes there by itself. Heal the network and restart Gitea. East is left writable with its writes.
5. Users open some lost issues again on west, and new issues, comments, labels and reactions land there, reusing issue numbers east already handed out. Their events join the paused backlog.
6. Add the cross-region latency and start the live users. Gitea, still in east, now reaches its database across the link.

Receipts live in the conductor, outside the cluster. The agent sees a short app description: a live replica in a second region managed by Orchestrator, users who expect it to stay available and responsive and to keep everything they do, and downstream systems that act on every issue event. It does not name the regions, the webhook path or the fault.

## What the agent has to do

- Keep one writable primary, the write service on it, and Orchestrator in agreement.
- Bring east's lost writes back through Gitea under the numbers the primary gives them: issues, comments, closes, labels, assignees, milestones, reactions. Adopt the repository that exists on disk but not in the database, with its issues and comments.
- Make Gitea's branch records match Git again.
- Drain the webhook backlog so each event arrives once under the issue's current number. Resuming it as is delivers wrong and repeated events; clearing it loses current ones.
- Rebuild east from west and move the primary back to east, beside Gitea, without Orchestrator undoing it, and leave recoveries enabled.
- Replay any webhook Gitea could not mark delivered while the old primary turned read-only during the switch.
- Do it without taking down the live database or Gitea.

## Grading

In order, stopping at the first failure:

1. Gitea serves, keeps one replica, and still uses `gitea-db-rw`. The repository volume, receiver and live users pods are unchanged.
2. A fresh write round-trips through Gitea and Git.
3. Replication and counters:
   - exactly one region is writable, and the write service selects it;
   - the other region replicates from it with no filters or skipped errors, and table checksums match;
   - issue counters, comment and closed counts, and label and milestone counters agree with the rows.
4. No acknowledged live user write was lost, no gap without one was longer than 10 seconds, and the median of the last 10 writes took at most 1.5 s.
5. Data and delivery:
   - every issue from both histories is on both regions, with every comment, close, label, assignee, milestone and reaction;
   - the relay is resumed and empty;
   - every event implied by the primary's rows was delivered once, with no duplicates or conflicting payloads;
   - recovered issues and comments are in Gitea's search index;
   - the repository created in the partition serves its files and holds each of its issues once, with their comments;
   - branch records match Git.
6. Orchestrator is running, records the serving primary as master, and has recoveries enabled.
7. The non-serving MySQL replica remains read-only and replicating after a pod restart.

Rows written straight into MySQL are not indexed and do not move counters, so they fail.

## Reference repair

Follows GitHub's order:

1. Disable Orchestrator's recoveries and pause the relay.
2. Save what only east has.
3. Drop queued events that no longer match the primary or repeat one.
4. Rebuild east from west.
5. Run `graceful-master-takeover-auto` to east, persist east writable and west read-only, and re-enable recoveries.
6. Adopt the new repository and write the saved activity again through the API.
7. Sync branch records with the admin dashboard's "Sync missed branches" action.
8. Replay deliveries the switch left stuck undelivered.
9. Resume the relay.

## Scale

Tiers set the shared, orphaned and post-failover issues and the partition's other writes:

| Tier | Shared / orphaned / post-failover issues | Other partition writes |
|---|---|---|
| small | 200 / 120 / 60 | 200 comments, 60 closes, 200 labels, 120 assignees, 120 milestones, 100 reactions, 20 pushes, a repository with 8 issues |
| large | 600 / 360 / 180 | about three times the small tier |

## Validation

On CloudLab (Kind, 3 workers): no-op scores 0 and the reference repair scores 1. See the PR for timings.

## Not in this PR

GitLab, the GitLab database deletion problem (#1074), CodeHub, and any new application.

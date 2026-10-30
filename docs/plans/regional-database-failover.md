# Regional database failover with real MySQL replication (#1069)

Status: built and registered as `gitea_mysql_regional_failover_small` and `gitea_mysql_regional_failover_large`.

## What this is

A problem based on GitHub's October 2018 outage ([post-incident analysis](https://github.blog/news-insights/company-news/oct21-post-incident-analysis/)).

A 43 second network partition made Orchestrator promote a West Coast MySQL replica to primary. The East primary had acknowledged writes that never replicated, so after the network came back both sides held writes the other lacked. Webhook work piled up behind the outage. Recovery meant keeping both histories, rebuilding replication, and delivering the backlog without dropping or duplicating anything.

## What already exists

`gitea_regional_failover` and `gitlab_regional_failover` (from #1062) cover the same story, but they fake it. Promotion is a `pg_restore` of a dump taken earlier. There is no live replication, no partition, no lag, and no backlog. The GitLab one was solved 10/10 in the screen, so it is unregistered as saturated.

This problem keeps the Gitea app, the workload client and the divergence checks from that work, and replaces the fake with the real mechanism.

## Layout

One namespace, `gitea`. Regions are a `region` pod label, and the partition is a Calico policy that denies traffic between the two labels. No Chaos Mesh.

```
region=east:  MySQL gitea-db-east  --- async GTID replication --->  region=west: MySQL gitea-db-west
              Gitea (writes go through the gitea-db-rw service)       webhook receiver
              webhook relay  ------------------------------------->
              application client, live users
```

- `components/mysql.py`: one StatefulSet per region on the official `mysql:8.4.6` image, the `gitea-db-rw` service, replication, promotion, the partition, and checks.
- Gitea runs on MySQL through `GITEA__database__DB_TYPE=mysql`.
- Gitea sends its webhooks to a relay beside it, standing in for GitHub's job queue. The relay forwards them in order, and can be paused, resumed, listed and have items dropped.
- The webhook receiver checks Gitea's HMAC signature and records each delivery with the issue id, number and comment id.
- A live users pod opens an issue about every 2 seconds through Gitea, as a non-admin member with write access to one repository, with varied titles and bodies, so downtime is measurable.
- The receiver and the live users run in the hidden `khaos` namespace, so the agent cannot read their code, logs or records. The relay reaches the receiver by its service IP, and the partition policy matches it across namespaces.
- Setup and grading run their Gitea and MySQL checks from two idle client pods in `khaos`, so nothing the agent changes in `gitea` sits on the grading path. MySQL is queried by pod IP, so a re-pointed service cannot answer for a server.
- The agent's kubectl proxy refuses node proxying, which reaches every pod's kubelet, and refuses minting service account tokens, which would work without the proxy. It also blanks Calico IPAM entries that name pods in hidden namespaces.

## Fault lifecycle

1. Deploy, seed, add webhooks, and write shared issues. Wait until west has every GTID and every webhook was delivered.
2. Apply the partition. Wait for three failed probes in a row, standing in for Orchestrator.
3. Gitea keeps writing to east: new issues, comments on new and shared issues, and closes of shared issues. Their events queue in the relay, which cannot reach the receiver.
4. Pause the relay, as GitHub paused webhook delivery. Promote west, heal the network, point `gitea-db-rw` at west, restart Gitea. East is left writable with its writes.
5. Users open some lost issues again on west, and new issues and comments land there, reusing issue numbers east already handed out. Their events join the paused backlog, so it now holds current events, events that describe east's history under numbers west reused, and repeats of the same event.
6. Start the live users and record the identity of the helper pods.

Receipts live in the conductor, outside the cluster, so the agent cannot read or edit them. The agent sees a short app description that says the database keeps a live replica in a second region, that users expect the service to stay up, and that consumers expect each issue event once with current details. It does not name the regions, the webhook path or the fault.

## What the agent has to do

- Keep one writable primary and the write service on it.
- Bring east's lost issues, comments and closes back through Gitea, under the numbers the primary gives them.
- Drain the webhook backlog so each event arrives once under the issue's current number: resuming as is delivers wrong and repeated events, and clearing it loses current ones.
- Rebuild east as a clean replica.
- Do it without taking down the live database or Gitea.

## Grading

In order, stopping at the first failure:

1. Gitea serves, keeps one replica, and still uses `gitea-db-rw`. The repository volume, receiver and live users pods are unchanged.
2. A fresh write round-trips through Gitea and Git.
3. Exactly one writable region, the write service selects it, the other region replicates from it with no filters or skipped errors, table checksums match, and issue counters and comment and closed counts agree with the rows.
4. Every issue from both histories is on both regions. Every comment and close is present. The relay is resumed and empty. Every event implied by the primary's rows was delivered once, with no duplicates or conflicting payloads. A delivery the primary has no record of sending, such as one of the stale side's events, must still match the primary. Events the primary sent stay valid if their issue changes later, so an agent's own test issues do not count against it. Recovered issues and comments are in Gitea's search index.
5. No acknowledged live user write was lost, and no gap without one was longer than 10 seconds. The oracle pauses the users while it grades and does not count that pause.
6. The non-serving MySQL replica remains read-only and replicating after a pod restart.

Rows written straight into MySQL are not indexed and do not move counters, so they fail.

## Reference repair

Reads only the two databases and the relay. It pauses the relay, drops queued events that no longer match the primary or repeat an earlier one, re-creates issues, comments and closes that exist only on the stale side through the API, resumes the relay, then reloads the stale side from a dump of the primary and starts replication.

## Scale

Tiers set the number of shared, orphaned and post-failover issues: small is 30, 12, 12 and large is 200, 80, 80. Both fit the standard Kind cluster.

## Validation

On CloudLab: no-op scores 0 and reference repair scores 1 on both tiers. Restarting Gitea stops writes for 17 seconds and restarting the live database for 54, so both score 0 on availability.

## Not in this PR

GitLab, the GitLab database deletion problem (#1074), CodeHub, and any new application.

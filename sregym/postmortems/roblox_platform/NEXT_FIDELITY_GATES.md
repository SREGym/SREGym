# Roblox incident: next fidelity gates

This is an implementation checklist for the draft native environment, not a
claim that its current 18–20 minute agent runs reproduce Roblox's 73-hour
incident. The [Roblox return-to-service account](https://about.roblox.com/newsroom/2022/01/roblox-return-to-service-10-28-10-31-2021)
is the grounding source. Keep the existing clean/fault/agent traces as the
baseline for judging each change.

## Replace the cache placement shortcut

Today, every `cache-N` Nomad job is pinned to a fixed worker. Consul KV records
are checked during rollout, but the scheduler does not use them to choose a
node. An agent can correct all stale records in one script and replace pools
directly through Nomad. Implement a cache deployment service whose desired
topology, capacity accounting, and progress live in Consul KV and whose actual
Nomad jobs are derived from that state. A Consul snapshot restore must be able
to roll back this state while allocations continue running. The resulting
conflict should be observable through ordinary job events and KV inspection,
without an incident-specific label or repair command.

The postmortem's unhealthy node must be an actual worker that remains attractive
to this scheduler because its recorded free capacity is wrong. Replacement
allocations should fail there for a real host reason, while healthy nodes and
existing cache allocations continue serving. A repair must reconcile the KV
topology, exclude or repair that node, and verify each live allocation and cache
shard. The grader must check serving coverage, placement safety, persistent
data invariants, and progress. It should not require a particular command
sequence. Direct Nomad edits may be valid, but the deployment service must
reconcile them against its desired state so bypassing it is not a one-script
solution.

## Make bootstrap and player return executable

The present controller replaces pools quickly, and admission remains at 100%
throughout the incident. Model a standing-start cache bootstrap with actual
database reads, cache warmup, and dependencies between cache layers and
application services. The deployment tool should have its historical
incremental-update limitation as an operational constraint visible in its
behavior, not an artificial sleep. An unsafe parallel restart should produce
measurable database, cache, or service impact that an agent can diagnose and
repair. There must be an efficient safe path; task length should come from
state and coordination, not a fixed timer.

After core services recover, redirect players to maintenance and expose a
DNS-like cohort steering surface. Returning 10% more players should produce
real cache misses and backend load. A 0→100% jump should fail a measured safety
invariant when caches are cold, while a measured staged return can succeed.
Grade full admission only after sustained workflows, durable purchases,
receipts, placement coverage, and cache/database health pass at that load.

## Validate diagnostic depth separately from rollout duration

Keep the operator briefing symptom-only. Degrade telemetry through the same
Consul dependency that affects services, while preserving lower-level shell,
logs, native metrics, profiles, and bounded expert help. The current 6 GB
versus roughly 100 MB Raft-file contrast led agents to act on the slow voter
within four minutes; calibrate a less conspicuous but still native BoltDB
freelist pathology and validate that a clean leader remains healthy. Do not
hide root-cause facts with a fake label or prohibit legitimate investigation.

For each new revision, require repeated healthy baselines, two valid failing
pre-agent grades with data invariants intact, controls that isolate streaming
pressure from slow-leader storage, and at least three blind agent runs. Report
time to first correct mitigation, time to cache recovery, time to full player
admission, and final independent grade separately. An hours-long result counts
only if agents spend that time diagnosing or coordinating real recovery; waiting
for an arbitrary rollout timer is not success.

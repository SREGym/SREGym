# Roblox long-horizon environment: gap assessment and corrected target

Status: the original `roblox_consul` fixture remains the Tier 0 three-trial
baseline. A [native replacement](../sregym/postmortems/roblox_platform/README.md)
now implements independent application services, native Consul/Nomad/Vault,
real routing subscriptions, durable processing, and a native evidence surface.
It addresses the diagnosis shortcuts and modeled repair APIs described below.
The full ultra-long-horizon target and historical fidelity are still unvalidated;
the remaining target in this document is not a completion claim.

## Why the current fixture is short

The implementation removed much of the work it was supposed to measure:

- Nine containers and one player-request path replace a large dependency graph.
  Scheduling, secrets, telemetry, and workload generation share one small control
  process; there are no actual Nomad or Vault deployments.
- Profiles expose model variables naming the defects. They do not require
  collecting and interpreting incomplete native profiles across a fleet.
- Compaction takes two seconds and clears a number. Reconciliation updates a few
  KV entries. These APIs package the diagnosis and repair into convenient actions.
- Historical mode begins after containment and snapshot restoration, skipping
  the evolving initial incident and the decisions that create the recovery tail.
- The larger tier keeps the same graph and physical container count. Streaming
  delay is normalized by tier, and origin capacity grows proportionally to
  request rate. More player rows mostly increase warming time.
- There are few independent controllers, recovery jobs, service versions, and
  background side effects. A successful mitigation does not expose much new work.
- The briefing carries considerable hindsight, including a compaction remedy.

The Roblox report describes a 73-hour total outage with substantial discovery
and recovery work. It also says the underlying slow-leader mechanism was
understood after the incident. Our fixture makes that knowledge available at
entry. Source: [Roblox Return to Service](https://about.roblox.com/newsroom/2022/01/roblox-return-to-service-10-28-10-31-2021).

## Corrected implementation target

Build a substantial executable Roblox-like platform covering the systems that
participate in this incident. The public postmortem grounds the dependency and
failure mechanisms; proprietary service implementations and undocumented topology
must be labeled as laboratory approximations.

1. **A real operational control plane.** Run Consul, Nomad, and Vault as separate
   stateful services, with multiple schedulable worker hosts, distinct deployment
   configurations, service registrations, health checks, sessions, and secrets
   dependencies. Operators get shell access to operational hosts and standard
   configuration, log, profiling, and maintenance tools. Grading and fault
   orchestration remain outside that access boundary.
2. **An application with meaningful dependency depth.** Implement identity,
   sessions, player profiles, inventory, experience metadata, matchmaking, game
   allocation, persistence, and asynchronous jobs. Use separate cache pools,
   database shards, queues, and routing clients. Shared code is acceptable, but
   replicas must execute work, maintain independent state, and affect users.
3. **Mechanically grounded faults.** First attempt a native streaming/churn and
   BoltDB log-store reproducer. Measure actual contention, allocation, I/O, Raft
   behavior, and application impact. If deterministic instrumentation is needed,
   put it in the relevant execution/storage path and document it; replacing a
   field in a Python dictionary cannot be the recovery mechanism.
4. **An evolving incident.** Begin with symptoms and ongoing automation before
   containment. Internal load persists after external traffic is stopped.
   Interventions alter real state: snapshots can stale allocations, node changes
   can trigger rescheduling, failed restores can consume capacity, and retries
   can amplify load. Preserve the same causal incident across seed variations.
5. **A substantial recovery tail.** Restoring the control plane must leave real
   allocations, leases, cache shards, queued work, and service deployments to
   validate or repair. Restoration proceeds through asynchronous, inspectable
   jobs that can fail, resume, or conflict with automation. Cold-cache admission
   affects measured origin pressure and user workflows.
6. **Partial operational evidence.** Aggregate telemetry shares relevant failed
   dependencies. Host-local evidence remains available, with enough irrelevant
   traffic and unrelated healthy services to require scoped investigation.
   Runbooks describe normal operations and real limitations, without naming the
   active cause or handing out a task-specific sequence of repair commands.

An initial large-tier engineering target is dozens of meaningful services,
hundreds of instances, and several dependency layers, distributed across worker
hosts. Exact counts require capacity measurements and are not sufficient by
themselves. Use the available CloudLab resources; infrastructure scarcity should
not dictate a nine-container substitute for the requested task.

## Scaling dimensions and acceptance

Keep the causal failure fixed while separately varying service/worker count,
dependency depth, catalog churn, state volume, observability impairment, and
recovery fanout. Publish these dimensions rather than labeling a larger database
alone as a larger environment.

Before calling a tier long-horizon:

- Establish a healthy application under sustained, diverse user workflows.
- Demonstrate the trigger and downstream failures through measured system behavior.
- Demonstrate why plausible partial mitigations fail or leave consequential work.
- Require active investigation and dependent recovery decisions across the fleet;
  arbitrary sleeps and forced observation windows do not establish task depth.
- Validate preservation of user state and acknowledged work, plus sustained
  end-to-end correctness under renewed load and relevant leader/worker changes.
- Record time spent diagnosing, executing real recovery work, and waiting, as
  separate quantities. Preserve intervention history and failed hypotheses.
- Run fresh multi-hour-budget agent evaluations only after validating the richer
  environment. Three short successes on Tier 0 do not answer that question.

The existing isolation, lifecycle, trace capture, and outcome-checking scaffolding
can be reused. The fault mechanics, response surface, application graph, and
recovery execution need substantial replacement.

## First expanded-tier agent result

The first Codex trial on the native replacement passed its independent final
grade (240/240 workflows) after about 7 minutes 21 seconds of agent work. The
pre-agent grade failed (152/160). The agent found the two altered Nomad settings
within 52 seconds by comparing live jobs against normal job specifications in
its workspace, then rolled those settings back. The native environment adds
meaningful applications and durable outcome checks, but this result demonstrates
that the active incident is still easy to diagnose and repair. The next fault
iteration must make the Consul failure mechanistically consequential after any
simple config rollback and make fleet recovery a real dependency-driven process.
See the [trial details](../sregym/postmortems/roblox_platform/VALIDATION.md).

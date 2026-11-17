# SREGym 2.0: shared gateway cascade (Microsoft 365, 12 November 2026)

Source: Microsoft's preliminary Post Incident Report for EX1550543 / MO1550821,
published 15 November 2026. The final PIR is due 20 November 2026; revisit
this plan when it lands, especially for the bulkhead failure, which the
preliminary report only calls "a separate issue".

## What happened, as a causal chain

| # | Fact from the PIR | Kind |
|---|---|---|
| 1 | A fix built only for the gateway servers (July) added lock contention in a connection-handling process. Under some load it exhausts CPU and memory and the server stops responding. It is worse on newer, higher-core hardware, which load tests did not cover. | latent defect, months old |
| 2 | The day before, about 14% of gateway capacity went out of service for an unrelated reason. Normal maintenance was bringing it back. Headroom covered it. | capacity loss, harmless alone |
| 3 | 01:07: automated failover moved Teams from the alternate gateway onto the primary, which Exchange shares. Load rose and the traffic mix changed. | routine failover, harmless alone |
| 4 | 1+2+3 at US peak: requests queue up and servers stop responding. Isolated failures from 01:07 to 16:00 were auto-mitigated. | trigger |
| 5 | Traffic management sends users to healthy endpoints, which raises their load and repeats the failure. More than 80% of servers were down within two hours. | cascade via the resilience mechanism |
| 6 | A separate issue broke the bulkheads between capacity units and regions, so the cascade went global. | missing isolation |
| 7 | Teams, OneDrive and SharePoint failed out to the alternate gateway. Exchange could not, for scale reasons. | asymmetric escape |
| 8 | Restored servers were overwhelmed again by waiting traffic. Automated recovery was not designed for that state. | metastable |
| 9 | The defect was months old, so rollback, flighting and feature flags could not revert it. | no quick revert |
| 10 | Mail delivery queued. Repair item: extend the delivery window so delayed mail is not returned as undeliverable. | data at risk |

Things the responders tried that did not hold, all useful as tempting dead ends:
restarting servers (18:00, 20:30), bypassing the health-check components
(20:00), an alternative health check (21:00), an auto-restart loop (21:00),
returning servers to service (23:55: healthy, then flattened), and protocol
changes, redistribution and blocking (04:00: partial, not sustained). Scaling up
VMs (05:00) helped only somewhat. What finally worked: shedding monitoring and
probe load (01:15), disabling anomaly-driven auto-redirection (01:53), reverting
the connection-handling code path, then taking groups of servers out of rotation,
patching and restarting them, and returning them to service (07:20 onward).

## Why this is a good 2.0 problem

The current families are saturated (see
`docs/deathstarbench/difficulty-calibration.md`). This incident adds things the
saturated families lack:

- **The obvious suspects are wrong.** The two recent changes (the failover and
  the capacity loss) are visible and recent, and both are harmless alone.
  Reverting the failover does not recover a fleet that is already in the
  metastable state. The cause is a change whose rollout finished months ago and
  has no flag.
- **The resilience mechanism drives the failure.** Health checks, ejection and
  anomaly-driven redirection all amplify it. An agent that trusts automation
  loses.
- **Recovery is ordered and partly irreversible.** You have to stop the
  amplifiers and shed load before patched units go back in. Restarting into full
  load flattens them again. Meanwhile queued mail ages toward bounce, and a
  bounce cannot be undone.
- **Scaling up makes it worse.** Contention grows with cores, so raising CPU
  limits speeds up the collapse. That is a trap for "add resources".
- **The escape hatch is asymmetric.** Chat and files can move to the alternate
  gateway. Moving mail there overloads it and takes chat down with it.

## Environment

All names are neutral. No Microsoft, Exchange, Teams or Outlook strings reach
the agent. The application is `WorkspaceApplication` (namespace `workspace`), a
subclass of the shared Mattermost application, so it also gets PostgreSQL and
the shared durability checks.

```
 workspace-clients (khaos)                       workspace namespace
 mail IMAP idlers, chat websockets,  ──► mail-<r> / chat-<r> Services (r = a, b, c)
 probes, external sender, bounce sink        │ selector set by traffic-manager:
                                              │   steer-<r>=in pods, or app=edge-alt
                                              ▼
                        edge-gw-<r> StatefulSets (capacity units)   edge-alt (HAProxy)
                                              │                          │
                 mailbox (Dovecot IMAP+LMTP) ◄┼──────────────────────────┤
                 mattermost (+ CNPG Postgres) ◄┘                          │
 mail-transport (Postfix) ── LMTP via mail-a:1024 ──► gateway ──► mailbox
                          └─ bounces to partners-mx (ExternalName → khaos sink)
```

- **`edge-gw`** (`docker/apps/workspace/edge-gw`, Go) is one StatefulSet per
  region, and each pod is a capacity unit. Its routes are IMAP :1143,
  LMTP :1024 and chat :8065. A probe listener (:8081) answers only after
  admission. The admin port (:9100) serves `/metrics` and `/livez`. There is
  no profiler, and tracebacks are off, so the evidence is metrics and
  behavior. Admission is a property of the release, not a setting:
  - 2026.06.4 (build tag `release2606`): sharded per-connection state. It
    rejects the `worker_threads` and `admission_timeout` keys as unknown.
  - 2026.07.2, 2026.09.1 and 2026.11.1: the July change. One spinlock guards
    the live-session table, and a hashing scan of the whole table runs while it
    is held. 2026.09.1 added the tuning keys; `worker_threads = auto` sizes
    GOMAXPROCS to host CPUs, which makes the spinning more expensive.

  At deploy, each StatefulSet is rolled through all four releases in order, so
  `kubectl rollout history` shows the real release trail. Rolling back the
  latest release, or the one before it, changes nothing. The fix is going back
  to 2026.06.4, and that release crash-loops on the current config until the
  tuning keys are removed. This is the PIR's "rollback, flighting and feature
  flags could not quickly revert the change".
- **`traffic-manager`** (`sregym/service/apps/workspace/runtime/traffic_manager.py`)
  re-reads the `traffic-policy` ConfigMap every cycle. It probes every unit,
  ejects failing ones and readmits recovered ones, then picks the serving units
  for each region. The policy settings are `max_ejection_percent`,
  `spillover regional|global`, `anomaly_redirect`, `drained` units and
  per-service `placement` (primary or alternate). It applies the choice as pod
  labels and Service selectors, and exposes `/status` and `/metrics`. Its logs
  narrate every ejection and redirect.
- **The drift before the incident**: `max_ejection_percent = 100`,
  `spillover = global`, `anomaly_redirect = enabled`. The `operations-log`
  ConfigMap records them among other ordinary changes, and also records the
  shared-session-cache rollout 128 days earlier.
- **Mail** runs real Postfix and Dovecot (`docker/apps/workspace/mail`). The
  `maximal_queue_lifetime` of 30m is a tenant policy in the
  `mail-transport-site` ConfigMap, applied at start. Postfix's queue lives on a
  volume.
- **Hidden clients** (`workspace-clients` in `khaos`) are the user
  population, with real IMAP LOGIN/SELECT/IDLE and real Mattermost websockets.
  They reconnect after a jittered retry. Probes run every 2 s per service and
  region. The external sender records every accepted Message-ID, and the bounce
  sink records every returned one. A control API resizes the population.

## Fault lifecycle

1. Deploy with release 2026.11.1 everywhere (after rolling through the history). At the daily peak, with
   chat on `edge-alt` and every unit in service, units hold about 230 sessions
   with no admission timeouts. That is the three months of clean operation.
2. Hold the baseline population until every probe passes.
3. Take one unit (`edge-gw-b-2`, 11% of the fleet) out of rotation and log it
   as a NIC fault.
4. Move chat from `edge-alt` onto the fleet as an automated failover.
5. Ramp to the peak in four steps. The units of the drained region tip first.
   Anomaly redirection and global spillover carry their load to the other
   regions, and every unit is ejected. `inject_fault` returns once every unit
   in service has been ejected and at least two regions' mail probes fail,
   and the state has held for 90 s. This takes about 7–10 minutes.

Calibration on Kind, single tier: 3 units per region at 150m CPU, peak mail
700 and chat 200 per region, a 60 s session hold, and clients that back off
exponentially up to 30 s. A unit tips at about 290 sessions.

## Grading (mitigation oracle)

`WorkspaceRecoveryOracle` checks behavior wherever it can, so there is no
single prescribed recipe.

1. **Live users**: within 5 minutes, every mail and chat probe in every region
   reaches ≥99% over 120 s at peak population. Shedding or blocking clients
   fails this check.
2. **Mail**: the transport queue drains, and every accepted message is
   delivered exactly once with zero bounces (checked against Dovecot through
   `doveadm fetch -A`).
3. **The defect is gone**: a hidden burst at 1.6× peak for 4 minutes. Probes
   must stay at or above 97%. Release 2026.06.4 passes easily. The
   shared cache collapses even with chat moved to `edge-alt`, so shedding load
   is not enough.
4. **One unit under load**: from the hidden namespace, 700 long-lived IMAP
   connections are held on one unit per region for 150 s, on top of its users.
   After a 90 s ramp, at most 2% of attempts may fail, and at least 95% of
   connections must stay up. This is the load test the PIR says was missing.
   2026.06.4 holds 700/700 at a p99 of a few milliseconds. The shared-table
   releases fail it whatever `worker_threads` is set to: with one thread,
   only about 250 connections held. That closes a hole the first hardened
   validation found, where one worker thread plus chat on `edge-alt` passed
   the fleet burst.
5. **Automation still works**: one unit in region a is blackholed with a
   NetworkPolicy for 90 s, and the region's probes must stay at or above 95%.
6. **The bulkhead holds**: all of region c is blackholed for 120 s, and
   regions a and b must stay at or above 95%.
7. The shared Mattermost/PostgreSQL checks pass: data, replicas and volumes.

The diagnosis judge expects the shared session cache to be named as the
defect, with the drain and the failover named as contributing load, and the
cascade attributed to ejection plus redirection across loosened bulkheads.

Live validation on Kind (`tests/integration/apps/workspace/validate_gateway_cascade.py`)
must show three things. No-op fails (`users_still_failing`). A partial fix
fails: it sets one worker thread, rolls back to the previous release
(2026.09.1), moves chat to `edge-alt` and returns the drained unit. Users
recover, but the per-unit load test fails (`unit_fails_load_test`, about 200 of
700 connections held). The reference repair passes: 1,573 external messages
were each delivered exactly once with no bounces, and the burst, unit-load,
unit-loss and region-loss checks held.

## Difficulty

First cohort (2026-11-17, before hardening): 3/3 passes, each fixed in about
90 s, by reading a ConfigMap comment that documented both admission modes and a
pprof dump that named `sharedCache` and `spinLock`. The hardening removed both
signposts, moved the defect into the release line, and shortened the mail
lifetime to 30 minutes.

Second cohort (2026-11-17, hardened; frozen tree `4808ac3f`; Codex
`gpt-6.1-sol`, 60-minute budget): 0/3 passes, all three attempts valid. Every
attempt diagnosed CPU throttling from `worker_threads = auto` (eight workers on
a 150m quota), with the drain, the chat failover and the cascade correctly
described. The judge scored 0.67, which is a fail, because none named the
release defect. Every fix set one worker per unit, moved chat to `edge-alt`
and contained redirection, without rolling back a release. Users recovered,
but each attempt failed the per-unit load test: a tuned shared-table unit holds
only 118–219 of 700 connections. Each attempt spent 1–2 minutes on diagnosis
and 0.5–1.5 minutes on mitigation, then stopped on green signals.

The first screen is three Codex attempts on `gpt-6.1-sol` with the single
tier, a frozen problem and grader, and a 60-minute agent budget. Results are
recorded separately, with infrastructure failures excluded.

## Fidelity limits

- Windows' internal process is replaced by a Go negotiation table. The
  mechanism (shared-lock contention that grows with cores and load) is kept.
- Thousands of capacity units become about a dozen, and time is compressed
  roughly 60:1.
- In the PIR, restored servers were flattened again by waiting traffic. Here
  that only happens while clients retry aggressively. With the clients'
  exponential backoff, a full restart recovers a shared-cache fleet at normal
  peak load, but it stays fragile, and the grader's burst exposes that.
- The PIR does not describe the bulkhead failure. It is modeled as policy
  drift until the final PIR says otherwise.

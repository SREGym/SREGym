# Mattermost capacity cascade: a real metric that means the wrong thing

`mattermost_capacity_cascade_single` and `mattermost_capacity_cascade_replicated`
are modelled on the shape of the [Slack 2021 outage](https://slack.engineering/slacks-incident-on-2-22-21/),
and are the first family here that is **not a data-recovery task**. Nothing is
lost and nothing needs restoring. The work is distrusting a correct measurement
and stopping automation that is actively making the outage worse.

It also gives Mattermost — previously the only application in this workstream
with no incident family — its first one.

## The cascade

All customer traffic reaches Mattermost through `chat-gateway`, which serves each
request from a bounded pool of worker threads. The only thing injected is added
latency on the gateway's upstream call. Everything after that is emergent:

1. Workers spend their time **blocked** rather than working, so throughput
   collapses and requests are shed.
2. Because each request costs real CPU and far fewer complete, the gateway's
   **CPU utilization falls**. This is a true measurement, not a broken one.
3. `capacity-scaler` is keyed on CPU. It reads idle-looking workers as spare
   capacity and **removes gateway replicas**, walking down to its own floor of 1.
4. A responder who scales the Deployment back up has it undone at the next
   decision, roughly 15 seconds later.

The fault injection asserts that step 3 actually happened — that the automation
reduced the gateway below its floor on its own — so a run where the cascade does
not develop fails loudly instead of grading an easier task.

| Dimension | `single` | `replicated` |
|---|---:|---:|
| Service capacity floor (gateway replicas) | 3 | 3 |
| Worker threads per gateway pod | 8 | 8 |
| Concurrent customer clients | 24 | 24 |
| Capacity automation interval | 15 s | 15 s |
| Injected upstream latency | 2,500 ms | 2,500 ms |
| PostgreSQL members | 1 | 3 |
| Persistent volumes | 3 | 5 |

## Impaired observability, on purpose

The surviving dashboard at `capacity-scaler:8080/dashboard` shows **only** CPU
and replica panels, and names its unavailable panels (`request_latency`,
`worker_saturation`, `error_rate`). Read alone, it tells a coherent and entirely
wrong story: CPU is low and falling, so capacity is being trimmed correctly.

The truth is available, but only by going to the source: `chat-gateway:8080/metrics`
reports worker saturation, shed requests and measured upstream latency, and the
gateway's pod logs carry the same on every request. `/control/scaler-decisions.jsonl`
records every capacity decision with the value it acted on.

## What counts as recovery

The grader asks for an outcome: customer-visible latency inside budget, no shed
requests, and capacity held at or above the floor for longer than two automation
intervals. What makes this a cascade rather than a one-line fix is that **the
order of the two obvious actions matters**:

- Scale the gateway up while the latency is still there, and the automation
  reverts it within about 15 seconds — CPU is still low, so it still reads the
  service as idle. This is the fix a responder reaches for first, and it fails.
- Remove the latency but leave capacity where the automation parked it, and the
  service is still below its floor.
- Remove the latency *and* restore capacity, and it holds: with the trigger gone,
  CPU reflects real load again and the same policy leaves the service alone.

So stopping or re-keying the automation is **not strictly required** — it is one
way to make capacity hold, and the way that works regardless of order. An agent
that fixes the trigger first and then restores capacity also passes. What cannot
pass is scaling up and declaring victory.

The reason codes separate the shortfalls:

- **`gateway_latency_unresolved`** — the trigger is still there. Graded from
  customer-visible p50 latency against a budget calibrated from healthy latency
  measured before injection, not an absolute number that depends on host speed.
- **`gateway_shedding_requests`** — customers are still being turned away.
- **`gateway_capacity_below_floor`** — capacity never reached the documented floor
  of 3 replicas, below which one rollout or node loss takes chat down. Capacity is
  counted in *ready* replicas, but a scale-up is given a bounded grace period to
  finish rolling out first, so being graded a few seconds after `kubectl scale` is
  not itself a failure.
- **`capacity_automation_still_shrinking`** — capacity was at the floor and then
  taken away again during the observation window. **This is the one that catches
  a manual scale-up while the trigger is still present.** Capacity is watched for
  longer than two automation intervals, so a fix the policy is about to undo
  cannot pass by being sampled at the right moment.
- **`gateway_missing`** — deleting the gateway removes the symptom by removing
  the service.

Plus the shared `SaaSOracle` checks: retained messages and attachments, a working
fresh message, original volumes, and the configured PostgreSQL membership and
synchronous durability.

There is deliberately **more than one valid repair**, because the grader asks
about the outcome rather than the method: disable the policy (`enabled: false`),
raise its floor to the service floor (`min: 3`), re-key it onto saturation
(`metric: "saturation"`), delete the scaler Deployment, or simply remove the
latency before restoring capacity. Reference recovery removes the latency and
re-keys the policy onto saturation.

## Calibration, so the task travels between hosts

Absolute CPU thresholds would encode how fast this particular machine is. Instead
the environment measures real healthy gateway CPU at deploy time and derives the
policy from it — scale in below half of healthy, scale out above 1.8× healthy —
recording the calibration in `/control/capacity-policy.txt` as an operator would.
Healthy load then sits inside the band, while a blocked pool falls far below it.
The policy is stored in a ConfigMap so a redeploy does not recalibrate against
incident state.

## What this does and does not reproduce

Real: a bounded worker pool, genuine CPU measurement that falls under saturation,
automation with real Kubernetes RBAC that actually patches the Deployment's scale
and actually reverts manual changes, persistent control state that survives pod
restarts, and impaired dashboards.

Not real: the latency is applied by the gateway itself reading a control file
rather than by network packet loss, so this does not exercise kernel or CNI
behaviour. There is one cluster on one host. Slack's later failure — the scale-up
itself breaking provisioning and terminating responders' access — is **not
modelled**; this family stops at the wrong-direction autoscaling and the capacity
restoration.

The scaler holds least-privilege RBAC: list and get pods, and get/patch/update
`deployments/scale` for `chat-gateway` only. It is the only pod in these
prototypes with an API token, and it has no access to secrets.

## Run admission

```sh
PYTHONPATH=/opt/sregym python tests/integration/validate_mattermost_cascade.py \
  --tier single --output results/cascade-admission.json
```

It requires healthy operation, then a cascade that develops on its own, then
rejects a manual scale-up as `capacity_automation_still_shrinking`, then rejects
removing the latency while capacity is still held down, then accepts a full
recovery, and finally confirms it survives a gateway restart.

The three-attempt comparison, once admission passes:

```sh
python scripts/evaluate_deathstarbench.py \
  --applications mattermost --incident capacity_cascade \
  --tiers single replicated --agent claudecode --model claude-opus-4-8 \
  --attempts 3 --profile svelte --agent-timeout 900 \
  --output results/cascade-comparison
```

Admission is not a difficulty result. For the agent screen, see below and the
[calibration report](difficulty-calibration.md) for the cohort rules.

## Claude Code screen: 3/3 passed, difficulty 0%

`claude-opus-5`, CLI `2.1.286`, `svelte`, mitigation only, 900-second budget.
The lifecycle gate passed 8/8 before any attempt was spent.

| Attempt | Verdict | Agent time | Final capacity | Customer p50 | Shed |
|---:|---|---:|---|---:|---:|
| 1 | pass | 237.7 s | 3/3 | 25.4 ms | 0 |
| 2 | pass | 206.3 s | 3/3 | 27.0 ms | 0 |
| 3 | pass | 443.2 s | 3/3 | 18.3 ms | 0 |

Median 237.7 s of a 900-second budget. All three attempts completed and graded
cleanly — no ambiguous verdicts, no environment errors.

The passes are substantive. Every attempt restored capacity to exactly the floor
and held it across the automation's decision window with zero requests shed, so
none of them took the manual-scale-up shortcut that
`capacity_automation_still_shrinking` exists to catch. The misleading CPU signal,
the impaired dashboard and the automation that reverts a naive fix did not
prevent a frontier agent from solving this in a third of its allowance.

## Completed live admission

The `single` tier passed full admission in 758 seconds with no errors and clean
cleanup. Calibration measured real healthy gateway CPU at **90.53%**, giving
`scale_in_below: 45` and `scale_out_above: 163`.

| Elapsed | Stage | Verdict |
|---:|---|---|
| 161 s | healthy | pass |
| 282 s | cascade developed on its own | `gateway_capacity_below_floor` (automation reached 1/1) |
| 300 s | manual scale-up, rollout complete | `capacity_automation_still_shrinking` (3/3 → 2/2) |
| 404 s | latency removed, capacity left alone | `gateway_capacity_below_floor` (parked at 2/2) |
| 486 s | automation stopped, latency restored | `gateway_latency_unresolved` (p50 2,512 ms vs a 500 ms budget) |
| 541 s | latency removed and capacity restored | pass |
| 607 s | after a gateway restart | pass |
| 715 s | reference recovery | pass |

The scaler's own decision log records the whole cascade, and it is the clearest
statement of what this family tests:

```text
observed 89.76% → "3 -> 3: cpu 89.76% within band"
observed 93.06% → "3 -> 3: cpu 93.06% within band"
observed 90.97% → "3 -> 3: cpu 90.97% within band"
observed 88.14% → "3 -> 3: cpu 88.14% within band"
observed  2.47% → "3 -> 2: cpu 2.47% below 44%"     # the latency lands
```

The same policy that leaves a healthy service alone strips a failing one, because
only the signal changed. After the manual scale-up it logged
`"3 -> 2: cpu 2.96% below 45%"` — the responder's fix undone, with its reason
stated.

The `latency_only` control is sound by arithmetic rather than by luck. Healthy
per-pod CPU is *H* at three replicas, so two replicas carry 1.5·*H* while
scale-out sits at 1.8·*H*: the automation climbs from 1 to 2 and stops, and can
never restore the floor by itself. The run observed 137% against a 163%
threshold, as predicted.

The recovery was accepted via `min: 3` rather than by disabling or re-keying the
policy, confirming that the grader takes any repair that holds capacity. Capacity
settled at 5/5 — above the floor, which passes — because CPU spiked past the
scale-out threshold while the latency backlog drained.

`control_state_persisted` confirms the injected condition lives on the persistent
volume: the gateway was restarted and the control file still held the value the
recovery left.

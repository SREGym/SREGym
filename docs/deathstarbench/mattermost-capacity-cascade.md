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
- **`gateway_capacity_below_floor`** — capacity is under the documented floor of
  3 replicas, below which one rollout or node loss takes chat down.
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

Admission is not a difficulty result. This family has **not** been screened
against any agent; see the [calibration report](difficulty-calibration.md) for the
cohort rules.

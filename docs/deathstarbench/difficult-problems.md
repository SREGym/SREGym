# The problems that separate a frontier agent

Generated 2026-10-06. Machine-readable companion:
[`difficult-problems.json`](difficult-problems.json), which carries every
attempt including the excluded ones.

Agent `codex` · model `gpt-6-astra` · CLI `0.160.0`.
Task description: generic SRE instruction plus the application description; no authored evidence.

Reproduce with `python scripts/screen_report.py results/clean-screen`.

| problem | solved | difficulty | median | shape | confidence |
|---|---|---|---|---|---|
| `gitlab_notification_delayed_audit_replicated` | **0 of 2** | 1.0 | 692s / 900s | race | solid |
| `gitlab_notification_intermittent_replicated` | **1 of 3** | 0.667 | 797s / 900s | race | solid |
| `gitlab_notification_unannounced_ambiguity_replicated` | **2 of 3** | 0.333 | 495s / 900s | unverified action | weak |
| `stripe_feature_config_single` | **2 of 3** | 0.333 | 237s / 900s | skipped verification | weak |

## Per-attempt detail

### `gitlab_notification_delayed_audit_replicated`

GitLab notification delivery, receipts published late.

Two valid attempts, both failed. A third hit the agent timeout and is excluded.

```
attempt 1  NOT SOLVED  586s  notification_delivery_violation
            {'notifications': {'expected': 30, 'delivered': 30, 'missing': 0, 'duplicates': 7, 'wrong_deliveries': []}}
attempt 2  EXCLUDED  (agent_timeout)
attempt 3  NOT SOLVED  798s  notification_delivery_violation
            {'notifications': {'expected': 30, 'delivered': 30, 'missing': 0, 'duplicates': 6, 'wrong_deliveries': []}}
```

### `gitlab_notification_intermittent_replicated`

GitLab notification delivery, SMTP still failing during recovery.

Two failures in three. The strongest evidence in the suite.

```
attempt 1  solved      797s
attempt 2  NOT SOLVED  844s  notification_delivery_violation
            {'notifications': {'expected': 30, 'delivered': 30, 'missing': 0, 'duplicates': 5, 'wrong_deliveries': []}}
attempt 3  NOT SOLVED  636s  notification_delivery_violation
            {'notifications': {'expected': 30, 'delivered': 30, 'missing': 0, 'duplicates': 3, 'wrong_deliveries': []}}
```

### `gitlab_notification_unannounced_ambiguity_replicated`

GitLab notification delivery, audit not named in the description.

One failure in three. Registered as a candidate for an n=10, not as an established result.

```
attempt 1  NOT SOLVED  451s  notification_delivery_violation
            {'notifications': {'expected': 30, 'delivered': 30, 'missing': 0, 'duplicates': 3, 'wrong_deliveries': []}}
attempt 2  solved      573s
attempt 3  solved      495s
```

### `stripe_feature_config_single`

Stripe payments edge configuration, with a dead webhook backlog.

One failure in three. Same evidence strength as the above; treat as a candidate.

```
attempt 1  NOT SOLVED  219s  webhook_backlog_incomplete
            {'member': 'stripe-marathon-db-1', 'changed_records': {}, 'duplicate_financial_effects': 0, 'missing_events': [], 'undelivered_events': ['evt_K1XMwVBS
attempt 2  solved      237s
attempt 3  solved      241s
```

## Caveats

- Every attempt is Codex gpt-6-astra. No problem here has been screened on a second agent.
- Three of the four are rungs of one subclass chain in one environment (delayed_audit <- intermittent <- ambiguity), so this is two distinct incidents, not four.
- A pass rate at a fixed budget is partly a statement about the budget: the solved families in this suite reach 93% of theirs.
- Pre-correction campaigns (task descriptions that disclosed the diagnosis) are excluded; they are quarantined under results/_voided/.

## What did not work

Ten candidates were built specifically to add difficulty and none did; the
levers and their results are in
[`difficulty-calibration.md`](difficulty-calibration.md) and the reasoning is in
[`HANDOFF.md`](HANDOFF.md). The short version: every lever lost to the same
habit, because the agent verifies against an independent source. The problems
above work because the uncertainty they contain is irreducible -- a failed SMTP
DATA leaves no record anywhere of whether the send was accepted -- which is a
property to select postmortems for rather than one that can be added.


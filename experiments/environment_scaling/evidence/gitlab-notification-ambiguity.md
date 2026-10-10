# GitLab recovery with ambiguous notification delivery

Status: comprehensive admission passed; the final version passed 24 focused
tests and all eight ordinary lifecycle stages in 1,850.04 seconds, including
cleanup. The fixed three-attempt model screen finished with **3/3 passes**.
The requested difficulty target is not met.

`gitlab_notification_ambiguity_replicated` extends the
[notification-recovery environment](gitlab-notification-recovery.md) as a separate
opt-in problem. The earlier notification variant passed 3/3 Codex attempts, as
did the [Stripe configuration case](stripe-config-screen.md). These results are
retained unchanged.

The new fixture exercises a real transport ambiguity: the local SMTP provider
commits some messages but closes the connection before acknowledging success.
Other failed attempts do not commit. Real GitLab Sidekiq jobs remain retryable
in Redis. Restoring the database and replaying every failed job can therefore
duplicate an already accepted notification. Database sequence gaps also retain
the earlier risk of stale jobs referring to different restored issue rows.

The provider audit contains ordinary synthetic mail and returns at most forty
records per page. Its documented `items`, `next`, `snapshot` and `total` fields
allow complete, consistent traversal. The reference recovery reads this same
HTTP surface available to agents; the grader independently reads the protected
durable ledger. Wrong or duplicate accepted mail cannot be erased by a later
database repair. All recipients remain local synthetic addresses, with no
external mail forwarding.

The validated admission fixture has six previously confirmed deliveries, three
additional accepted messages whose SMTP acknowledgements were lost, twenty-one
undelivered intents, and twenty-four retained incident jobs. Setup must verify
these counts before admitting an episode. This is a synthetic extension of the
GitLab recovery family, not a claim about historical SMTP failures at GitLab.

Run admission in an otherwise idle DinD cluster with CloudNativePG installed:

```sh
python tests/integration/validate_gitlab_notification_ambiguity.py \
  --output results/notification-ambiguity-admission.json
```

After admission passes, run the fixed three-attempt screen:

```sh
python scripts/evaluate_deathstarbench.py \
  --applications gitlab_ce --incident notification_ambiguity --tiers replicated \
  --model gpt-6-astra --agent-version 0.157.1 --attempts 3 \
  --profile svelte --agent-timeout 900 --output results/notification-ambiguity-three
```

Local development evidence is under `results/dind/sregym-difficulty-baseline/`,
including `notification-ambiguity-source.json`, the source archive, protocol-test
log and admission output. All three attempts completed with valid grades and
successful cleanup; no infrastructure failure or ambiguous result was counted.

## Admission evidence and revisions

Revision 1 passed comprehensive admission in 2,217.28 seconds, including cleanup.
Seven real SMTP attempts failed: three had already been accepted, and four had
not. All twenty-four incident jobs remained queued or retryable. The public
audit exposed the complete 335-record ledger across pages. Reference recovery
delivered all thirty notifications exactly once, passed fresh work, preserved
the original data and eight volumes, and passed again after reconciliation and
provider restart. Deliberately resending an ambiguous intent and sending another
notification to the wrong recipient produced `notification_delivery_violation`.

Revision 2 limits the temporary injected SMTP fault to incident notifications.
This prevents unrelated welcome or digest mail from consuming its finite fault
budget during setup. A new socket-level regression test verifies that unrelated
mail remains accepted and acknowledged without consuming that budget. Grading,
pagination, database recovery and reference reconciliation are unchanged. The
final version passed a fresh ordinary lifecycle gate before any model trial,
including healthy grading, fault detection, reference recovery and cleanup.
The comprehensive revision-1 evidence is retained under explicit `v1` filenames.

Structured evidence is in [the result record](gitlab-notification-ambiguity-results.json).

## Model screen

All three attempts used `gpt-6-astra`, actual Codex CLI `0.157.1`, agent-default
reasoning, mitigation only, the `svelte` profile and a 900-second solving budget.
Agent times were 500.384, 464.991 and 423.248 seconds (median 464.991). Each grade
verified all three database members, eight original volumes, acknowledged data
with zero loss, all thirty notifications exactly once, and fresh notification
work. The twenty-four source hashes matched on host and in DinD after grading.

Agents reconciled the complete paginated provider audit. The third run preserved
original internal issue IDs, while other runs rebuilt notification work against
restored data. Both approaches satisfied the outcome-based grader. Grading-time
provider ledgers and hashed traces are retained for each attempt.

This synthetic extension adds real recovery hazards, but the observed failure
rate is **0/3**. Past SMTP ambiguity and audit pagination did not meet the target
of at least one failure in three. These exploratory results remain part of the
[calibration record](difficulty-calibration.md), including when later variants
are evaluated.

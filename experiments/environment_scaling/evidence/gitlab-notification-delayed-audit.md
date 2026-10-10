# GitLab recovery with delayed provider evidence

Status: revision 1 passed 37 focused tests and five subtests, Ruby syntax
validation, image/source verification, full live admission, and all three valid
Codex attempts. Agent times were 556.479, 450.626 and 510.729 seconds (median
510.729). Every teardown passed. All 34 source hashes matched on the host and in
DinD after the cohort completed. The requested difficulty target remains unmet:
all eighteen completed calibration attempts have passed.

`gitlab_notification_delayed_audit_replicated` extends the
[continuing-SMTP incident](gitlab-notification-intermittent.md) as a separate,
opt-in problem. It retains real GitLab CE, three PostgreSQL members, durable
Redis jobs, an older backup, acknowledged-write receipts and the independent
SMTP acceptance ledger. Initially nine notifications have been accepted and
twenty-one remain undelivered, with twenty-four retained incident jobs.

Accepted incident mail is durable immediately, but its public audit receipt
appears thirty seconds later. SMTP may close before acknowledging an accepted
send, or close without accepting it. Retrying because a recent audit snapshot
contains no receipt can therefore deliver the same notification twice.

The public audit exposes `observed_at`, `complete_through`, and
`publication_delay_seconds`. Its guide explains how to use a provider-clock
barrier to establish complete evidence without synchronizing the client's clock.
Pagination preserves the observation time and receipt set across every page.
Acceptance and publication schedules commit together and survive provider
restart. Normal mail and fresh-work probes are outside the impaired stream.

This is a synthetic extension for degraded incident telemetry, not a historical
claim about GitLab's mail provider. The acceptance ledger and outcome grader are
unchanged: preserve original data and volumes, deliver each acknowledged
notification once to the correct recipient with correct content, leave no unsafe
incident retries, and support fresh work.

The reference recovery pauses Sidekiq and reconciles batches using only the
public audit. Each missing intent is attempted once per batch; the reference
waits for complete evidence before selecting the next retry set. Admission must
demonstrate delayed visibility, safe reference recovery, repeated reconciliation,
provider restart, and rejection of deliberate duplicate/wrong-recipient sends.
Reference command timeout is 600 seconds; the model's solving budget remains
900 seconds, matching every preceding cohort.

Run comprehensive admission in an idle DinD cluster:

```sh
PYTHONPATH=/opt/sregym python tests/integration/validate_gitlab_notification_delayed_audit.py \
  --output results/notification-delayed-audit-admission.json
```

The comparison command performs its ordinary lifecycle gate before three fresh
attempts:

```sh
python scripts/evaluate_deathstarbench.py \
  --applications gitlab_ce --incident notification_delayed_audit --tiers replicated \
  --model gpt-6-astra --agent-version 0.157.1 --attempts 3 \
  --profile svelte --agent-timeout 900 --output results/notification-delayed-audit-three
```

The completed campaign used the comprehensive admission above as its gate, followed
by three fixed attempts with the same model, CLI and budget. Model trials cannot
start unless admission and namespace cleanup pass. Infrastructure and admission
failures cannot satisfy the difficulty target.

[Structured evidence](gitlab-notification-delayed-audit-results.json) records the
source manifest and local image. Raw artifacts are under
`results/dind/sregym-difficulty-baseline/`. The
[calibration report](difficulty-calibration.md) retains every preceding pass.

## Completed live admission

Comprehensive admission passed in 2,251.00 seconds, including cleanup. Healthy
operation passed; deletion, backup-only restoration and data-only restoration
failed for their expected reasons. Reference recovery and provider-restart
checks passed, and deliberate wrong/duplicate sends failed grading.

The reference removed 24 stale incident jobs and recovered 21 undelivered
intents in batches of 21, 5 and 2 attempts. Fourteen SMTP attempts raised transport
exceptions. All 21 accepted recovery receipts were absent from the first audit
read after their batch, then appeared after the completeness watermark advanced.
The reference spent 120.17 seconds waiting across four watermark barriers,
including its initial evidence check. All 30 required notifications ended up
correctly delivered exactly once, and fresh work passed.

A second reconciliation removed no jobs and sent nothing. After provider restart,
all 21 delayed publication schedules still had their original 30-second delay;
the continuing SMTP fault remained active with 28 attempts recorded. The fault
was disabled only for the final controlled negative-delivery probes, after all
safe recovery and restart checks. All three model attempts and their cleanups passed.

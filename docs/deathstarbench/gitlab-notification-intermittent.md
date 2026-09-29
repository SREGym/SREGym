# GitLab recovery during continuing SMTP failures

Status: 29 focused tests and full admission pass on frozen revision 2.
All three fixed model attempts passed. The requested difficulty target remains
unmet. The preceding
[ambiguity candidate](gitlab-notification-ambiguity.md) passed 3/3 Codex attempts,
so the requested difficulty target remains unmet.

`gitlab_notification_intermittent_replicated` is a separate, opt-in extension.
It retains the real GitLab database deletion, PostgreSQL replicas, older backup,
acknowledged issue journal, independently durable Redis jobs and SMTP ledger.
The initial fixture still contains nine accepted notifications and twenty-one
undelivered intents, with twenty-four retained incident jobs.

Unlike the preceding variant, SMTP transport failures continue during recovery.
Some failed sends have already been accepted; others have not. Successful sends
are interleaved with failures. The provider's paginated, read-only audit remains
available, and the incident operations log explicitly describes the continuing
impairment. Blind retries can produce irreversible duplicate mail after the
database itself has been repaired.

This is a deterministic synthetic fault model applied to incident traffic, not
a historical claim about GitLab's SMTP provider or a production failure rate.
The impairment and its counter persist across provider restart. Ordinary mail
and fresh-work probes remain outside the selected incident stream.

The existing outcome grader is unchanged: preserve original data and storage,
deliver every acknowledged notification once with correct content and recipient,
leave no incident jobs queued for unsafe later retries, and support fresh work.
The reference recovery pauses Sidekiq, reconciles stale incident jobs, sends
through GitLab's real `Notify` mailer, and consults the same public audit after
each send before deciding whether to retry. It never deletes accepted evidence.

Admission checks healthy grading, injected failure, insufficient backup-only and
data-only repairs, safe reference recovery, repeated reconciliation, provider
restart and deliberate wrong/duplicate delivery. It additionally checks that
both accepted and unaccepted transport failures occurred during recovery and
that the continuing fault survives restart. The fault is disabled only for the
final controlled negative-delivery probes, after recovery and restart checks.

Run comprehensive admission in an otherwise idle DinD cluster:

```sh
python tests/integration/validate_gitlab_notification_intermittent.py \
  --output results/notification-intermittent-admission.json
```

The standard comparison command performs its own ordinary lifecycle gate before
three fresh attempts:

```sh
python scripts/evaluate_deathstarbench.py \
  --applications gitlab_ce --incident notification_intermittent --tiers replicated \
  --model gpt-6-astra --agent-version 0.157.1 --attempts 3 \
  --profile svelte --agent-timeout 900 --output results/notification-intermittent-three
```

[Structured evidence](gitlab-notification-intermittent-results.json) records the
source manifest, image and validation status. Raw artifacts are under
`results/dind/sregym-difficulty-baseline/`. Infrastructure failures cannot satisfy
the target of at least one genuine agent failure in three valid graded attempts.

## Development admission

Revision 1 passed healthy, injected-fault, backup-only and data-only checks, then
aborted in the reference mail-delivery path during an SMTP failure. Cleanup
passed and no model trials started. Its source, image and failed admission are
retained under explicit `notification-intermittent-v1-*` filenames. This is a
validation failure, not evidence of agent difficulty.

Revision 2 consults the provider audit after any mail-delivery exception before
deciding whether to retry, with a bounded retry limit. Persistent application or
rendering failures still cannot establish accepted delivery and fail recovery.
Admission now retains both the beginning and end of exception output. The fault
policy and outcome grader are unchanged.

Full revision 2 admission passed in 2,091.69 seconds, including cleanup. The
reference made 28 sends to recover the remaining 21 notifications. Fourteen
raised `ApplicationMailer::SMTPConnectionError` with an `EOFError` cause: seven
had been accepted and seven had not. Reading the public provider audit avoided
duplicates. A second reconciliation sent nothing, and all 30 notifications
remained correct after provider restart with the ongoing fault still active.
Deliberate wrong-recipient and duplicate deliveries subsequently failed grading.
This successful admission gates the three model attempts.

## Completed fixed model cohort

| Attempt | Outcome | Agent time | Grading time | Cleanup time |
|---|---|---:|---:|---:|
| 1 | PASS | 664.321 s | 132.745 s | 263.920 s |
| 2 | PASS | 845.057 s | 134.230 s | 239.454 s |
| 3 | PASS | 580.621 s | 134.703 s | 262.483 s |

Median agent time was 664.321 seconds. All traces used `gpt-6-astra` and Codex
CLI `0.157.1`, with agent-default reasoning, mitigation only, the `svelte` profile,
and a 900-second solving budget. All three teardowns passed; no missing or
ambiguous grade occurred. The full model campaign took 6,516.85 seconds,
excluding the preceding comprehensive admission.

Every attempt recovered the thirty acknowledged issue receipts without loss,
verified three PostgreSQL members and eight original volumes, delivered all
thirty required notifications exactly once with correct content and recipient,
and passed fresh work. Protected grading-time ledgers contain thirty unique
incident deliveries per attempt. The continuing fault remained active with
28 send attempts in every run. The finite setup fault's unaccepted retry count
varied with Sidekiq timing; its accepted fixture and retained jobs were unchanged.

Attempt 2 additionally installed an application delivery guard and restarted
GitLab. It passed in 845.057 seconds, within the same 900-second budget. Safe
alternative recovery implementations are accepted by the unchanged outcome grader.

All 29 frozen source hashes matched on the host and inside DinD after the three
agent phases completed. The source manifest, archive, image record, admission,
raw traces, grades and mail ledgers are retained. The ordinary result root is
`results/dind/sregym-difficulty-baseline/0926_1419/codex/gitlab_notification_intermittent_replicated/`.
The prior candidate passes remain in the [calibration report](difficulty-calibration.md).
This adaptive screen does not estimate held-out reliability.

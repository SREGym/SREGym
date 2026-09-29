# Expanded GitLab recovery workload

Status: revision 1 passed 38 focused tests and five subtests, Ruby syntax checks,
image/source verification, and full live admission. The fixed three-attempt
Codex cohort has two valid passes, in 637.514 and 686.947 seconds. The third
invocation was rejected by the model service before Codex took any actions:
`gpt-6-astra` was reported unsupported for the configured ChatGPT account. A
minimal retry returned the same error. This is an invalid attempt, not evidence
of benchmark difficulty. Cleanup passed in 672.791 seconds. The final audit found
all 37 source hashes unchanged in both the host and DinD container. One valid
trial is still needed; the target is unmet.

`gitlab_notification_delayed_audit_expanded` scales the workload of the
[delayed-audit incident](gitlab-notification-delayed-audit.md), whose fixed
three-attempt Codex screen passed 3/3. This is an opt-in workload tier on the same
replicated deployment, with the same causal failure and outcome contract.

| Dimension | Replicated workload | Expanded workload |
|---|---:|---:|
| Incident tenant projects | 5 | 20 |
| Historical issues created before backup | 60 | 300 |
| Acknowledged issues and notification intents | 30 | 120 |
| Initially accepted incident notifications | 9 | 9 |
| Initially undelivered notifications | 21 | 111 |
| Retained incident jobs | 24 | 114 |
| Unrelated audit messages | 320 | 1,600 |
| PostgreSQL members | 3 | 3 |
| Persistent volumes | 8 | 8 |
| Provider publication lag | 30 s | 30 s |
| Codex solving budget | 900 s | 900 s |

The wrong-primary deletion still reaches all PostgreSQL replicas. The usable
backup predates acknowledged writes, and durable Redis jobs may point at stale
internal IDs. SMTP failures continue during recovery while acceptance receipts
appear late in the paginated provider audit. The agent must restore original
data and service, reconcile the larger backlog safely, and verify fresh work.

The reference reconciler allows up to eight bounded batches for the larger tail.
It uses the same public evidence and completeness watermark as the smaller
candidate. Its command timeout remains 600 seconds; the model and cleanup budgets
remain 900 seconds. Admission checks safe recovery, repeated reconciliation,
provider restart, and rejection of duplicate or wrong-recipient deliveries.
Because a larger batch takes longer to send, admission permits early receipts to
publish before that batch ends while requiring proof of delayed evidence at its
tail.

These notification impairments are synthetic extensions of GitLab's historical
recovery risks. This tier increases tenancy, records, backlog and telemetry; it
does not add geographic regions or claim to reproduce a production-sized fleet.

Run full admission in an idle DinD cluster:

```sh
PYTHONPATH=/opt/sregym python tests/integration/validate_gitlab_notification_expanded.py \
  --output results/notification-expanded-admission.json
```

The comparison command supports both workloads:

```sh
python scripts/evaluate_deathstarbench.py \
  --applications gitlab_ce --incident notification_delayed_audit \
  --tiers replicated expanded --model gpt-6-astra --agent-version 0.157.1 \
  --attempts 3 --profile svelte --agent-timeout 900 \
  --output results/notification-workload-comparison
```

The current calibration campaign uses comprehensive admission as its gate and
then runs three fresh expanded attempts with frozen source. Its predecessor's
three passes are retained in the [calibration report](difficulty-calibration.md).
Infrastructure or admission failures cannot satisfy the difficulty target. The
source stays frozen while model access is resolved. Any replacement model needs
its own three-attempt comparison rather than mixing models in this cohort.

[Structured evidence](gitlab-notification-expanded-results.json) records source
and image provenance. Raw artifacts are under
`results/dind/sregym-difficulty-baseline/`.

## Completed live admission

Full admission passed in 2,635.72 seconds, including cleanup. Healthy operation
passed. Deletion, backup-only restoration and data-only restoration failed for
their expected reasons. Reference recovery and provider restart passed; deliberate
duplicate and wrong-recipient delivery failed grading.

The fixture contained 120 notification intents, nine accepted deliveries, 111
missing deliveries, 114 retained jobs and 1,630 provider audit records. The public
paginated audit matched the protected ledger. Reference recovery removed the
114 stale jobs and used batches of 111, 28, seven and two attempts, for 148 sends.
Seventy-four attempts raised SMTP transport exceptions. Seventy-three accepted
receipts were absent in the first audit read after their batch and appeared after
waiting for complete evidence; earlier receipts in the longer batches had already
published. The reference waited 151.73 seconds across five watermark barriers.

All 120 required messages ended up correctly delivered exactly once, with fresh
work passing and zero acknowledged data loss. Repeated reconciliation removed
no jobs and sent nothing. After provider restart, all 111 delayed publication
schedules retained their 30-second delay, and the SMTP fault remained active with
148 attempts recorded. The fault was disabled only for the final controlled
negative-delivery probes.

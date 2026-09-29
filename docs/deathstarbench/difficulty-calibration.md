# Postmortem difficulty calibration

The requested target is **at least one genuine agent failure in three valid
graded attempts** on a fixed candidate. The initial three-run baseline was
completed before changing the problem. Infrastructure failures, missing grades
and development-time admission failures do not satisfy the target.

| Candidate | Valid attempts | Passes | Median agent time | Report |
|---|---:|---:|---:|---|
| GitLab database deletion, replicated | 3 | 3 | 420.7 s | [Baseline](gitlab-recovery-baseline.md) |
| GitLab database and notification recovery | 3 | 3 | 469.4 s | [Notification screen](gitlab-notification-recovery.md) |
| Stripe recurring feature configuration | 3 | 3 | 110.8 s | [Stripe screen](stripe-config-screen.md) |
| GitLab ambiguous notification delivery, revision 2 | 3 | 3 | 465.0 s | [Ambiguity screen](gitlab-notification-ambiguity.md) |
| GitLab recovery with continuing SMTP failures | 3 | 3 | 664.3 s | [Intermittent screen](gitlab-notification-intermittent.md) |
| GitLab delayed provider audit | 3 | 3 | 510.7 s | [Delayed audit screen](gitlab-notification-delayed-audit.md) |
| Expanded GitLab delayed-audit workload | 2 valid; 1 invalid | 2 | Pending third valid trial | [Candidate](gitlab-notification-expanded.md) |

The target is **not yet met**. All twenty valid graded trials above passed. The
expanded cohort's third invocation was rejected by the model service before any
agent actions; a minimal retry confirmed the account-access error. It is excluded
from the difficulty calculation, and one valid trial remains outstanding.
Each cohort kept its problem and grader unchanged. Every successful trace used
`gpt-6-astra` and Codex CLI `0.157.1`,
with agent-default reasoning, mitigation only, the `svelte` profile and a
900-second solving budget. The baseline report records the initially ignored
CLI override and the subsequent harness fix; its actual CLI version matches the
later screens. Deployment and grading time are excluded from agent time.

The ambiguity candidate adds real SMTP acknowledgement loss, retained
Sidekiq retries and a noisy, paginated provider audit. Its comprehensive admission
proved safe reference recovery and lasting rejection of duplicate and
wrong-recipient mail. A final setup guard scopes the temporary transport fault
to incident notifications. That final revision passed all eight ordinary lifecycle
stages before its three model trials began. All three passed. A separate
continuing-transport extension passed full admission and all three model attempts.
The delayed-audit extension passed full admission and all three model trials.
An expanded workload candidate passed full admission and has two valid passes in
its fixed three-attempt screen, with model access preventing the third valid
attempt. It increases tenancy, acknowledged writes, notification
backlog and audit volume while preserving the same fault mechanism, topology,
grading contract and solving budget.

These are exploratory calibration screens with adaptive candidate development.
They do not provide an independent held-out estimate of model reliability or
establish that replicas alone caused a difficulty change. Earlier successes are
retained when later candidates are added.

The [machine-readable calibration audit](difficulty-calibration-results.json)
recomputes counts and median agent times from raw CSVs and verifies model/CLI
metadata against saved traces. Detailed source hashes, grades, runtime versions, image records and traces are
linked from the individual reports. Raw local artifacts are under
`results/dind/sregym-difficulty-baseline/`. Code and artifacts remain local and
uncommitted; nothing has been pushed.

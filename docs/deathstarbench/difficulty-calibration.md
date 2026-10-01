# Postmortem difficulty calibration

The requested target is **at least one genuine agent failure in three valid
graded attempts** on a fixed candidate. The initial three-run baseline was
completed before changing the problem. Infrastructure failures, missing grades
and development-time admission failures do not satisfy the target.

Every row below is a **Codex** (`gpt-6-astra`) screen.

| Candidate | Valid attempts | Passes | Median agent time | Report |
|---|---:|---:|---:|---|
| GitLab database deletion, replicated | 3 | 3 | 420.7 s | [Baseline](gitlab-recovery-baseline.md) |
| GitLab database and notification recovery | 3 | 3 | 469.4 s | [Notification screen](gitlab-notification-recovery.md) |
| Stripe recurring feature configuration | 3 | 3 | 110.8 s | [Stripe screen](stripe-config-screen.md) |
| GitLab ambiguous notification delivery, revision 2 | 3 | 3 | 465.0 s | [Ambiguity screen](gitlab-notification-ambiguity.md) |
| GitLab recovery with continuing SMTP failures | 3 | 3 | 664.3 s | [Intermittent screen](gitlab-notification-intermittent.md) |
| GitLab delayed provider audit | 3 | 3 | 510.7 s | [Delayed audit screen](gitlab-notification-delayed-audit.md) |
| Expanded GitLab delayed-audit workload | 2 valid; 1 invalid | 2 | 662.2 s (2 trials) | [Candidate](gitlab-notification-expanded.md) |

The target is **not yet met**. All twenty valid graded trials above passed. The
expanded cohort's third invocation was rejected by the model service before any
agent actions; a minimal retry confirmed the account-access error. It is excluded
from the difficulty calculation.
Each cohort kept its problem and grader unchanged. Every successful trace used
`gpt-6-astra` and Codex CLI `0.157.1`,
with agent-default reasoning, mitigation only, the `svelte` profile and a
900-second solving budget. The baseline report records the initially ignored
CLI override and the subsequent harness fix; its actual CLI version matches the
later screens. Deployment and grading time are excluded from agent time.

## The Codex expanded cohort is closed at two valid passes

`gpt-6-astra` is not entitled for the configured ChatGPT account, so the third
valid Codex attempt cannot be obtained on this host. Rather than leave the cohort
open indefinitely, evaluation moves to the **Claude Code** client, which is
authenticated here.

This does not extend the expanded cohort. An agent change is a new cohort: the
comparison runner now takes `--agent`, and a Claude Code screen needs its own
three attempts on the frozen problem before any difficulty claim. The Codex rows
above are retained as what they are — passes by a different agent — and must not
be averaged with Claude Code attempts. Nothing about the environments, faults or
graders changes with the client.

Both drivers now build their prompt from the conductor's active stage. The
Claude Code driver previously sent a diagnosis-first prompt on a mitigation-only
attempt, which instructs the agent to submit before repairing anything; a screen
run through that path would have measured the prompt, not the environment.

The ambiguity candidate adds real SMTP acknowledgement loss, retained
Sidekiq retries and a noisy, paginated provider audit. Its comprehensive admission
proved safe reference recovery and lasting rejection of duplicate and
wrong-recipient mail. A final setup guard scopes the temporary transport fault
to incident notifications. That final revision passed all eight ordinary lifecycle
stages before its three model trials began. All three passed. A separate
continuing-transport extension passed full admission and all three model attempts.
The delayed-audit extension passed full admission and all three model trials.
An expanded workload candidate passed full admission and has two valid passes in
its Codex screen; model access prevented the third. It increases tenancy,
acknowledged writes, notification backlog and audit volume while preserving the
same fault mechanism, topology, grading contract and solving budget. Its
remaining attempts will be run under Claude Code as a separate cohort.

## Next candidate: regional failover divergence

Every candidate above is recovered by restoring the right artifact and replaying a
tail. The [regional failover family](gitlab-regional-failover.md) deliberately
removes that shape: two histories each hold acknowledged writes, so failing back
and doing nothing each fail with their own reason code, and only reconciling both
passes. It is implemented, unit-tested, and has **passed full live admission** on
its `single` tier in 1,963 seconds — both negative controls, a partial
reconciliation, a duplicate, an application restart and unchanged evidence, with
clean cleanup. It has **not** been screened against any agent.

The [Mattermost capacity cascade](mattermost-capacity-cascade.md) removes the
restore-and-replay shape differently again: nothing is lost, and the agent has to
distrust a correct CPU measurement and stop automation that undoes a manual
scale-up. It is implemented, unit-tested, and has **passed full live admission**
on its `single` tier in 758 seconds — including a cascade that developed on its
own, a manual scale-up rejected because the automation undid it, both single-sided
repairs rejected, a gateway restart and persistent control state. It has **not**
been screened against any agent.

## Claude Code screens of the two newest families

Both new families were screened with **Claude Code** (`claude-opus-5`, CLI
`2.1.286`, `svelte` profile, mitigation only, 900-second budget). This is a
separate cohort from the Codex rows above and must not be pooled with them.

| Candidate | Lifecycle gate | Valid attempts | Passes | Median agent time | Difficulty |
|---|---|---:|---:|---:|---:|
| Mattermost capacity cascade, single | 8/8 pass | 3 | 3 | 237.7 s | **0%** |
| GitLab regional failover, single | 8/8 pass | 3 | 3 | 263.5 s | **0%** |

All six attempts completed and graded cleanly: no ambiguous verdicts, no
environment errors, no missing grades, and `agent_exit_code` 0 throughout. The
difficulty target — at least one genuine agent failure in three valid attempts —
is **not met by either family**.

The passes are substantive rather than lucky. Every cascade attempt restored
capacity to exactly the service floor and held it across the automation's
decision window with zero requests shed, so none took the manual-scale-up
shortcut that `capacity_automation_still_shrinking` exists to catch. Every
failover attempt recovered all six orphaned writes *and* retained all six
post-promotion writes with zero acknowledged data loss, so none failed back and
none abandoned the orphans.

Agent time is the clearest signal: a median of 238 s and 264 s against a
900-second budget means both families were solved in roughly a quarter to a third
of the allowance, faster than the Codex medians on the older and simpler families
above. Designing a task so that no single restore recovers it, or so that a
correct metric points the wrong way, did not make it hard for a frontier agent.

Two harness defects had to be fixed before any of this could run, and both would
have produced invalid attempts rather than difficulty data: the campaign runner
could not select either new incident, and the Claude Code client rejected
subscription credentials outright. See the commit history.

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

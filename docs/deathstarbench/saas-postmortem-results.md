# SaaS postmortem validation results

All four incident/tier cases passed live admission inside the private DinD
cluster on one machine. The normal Stripe conductor lifecycle also passed.
These are environment and grader checks. Subsequent Codex evaluation of the
replicated GitLab deletion case passed all three attempts; see the
[baseline report](gitlab-recovery-baseline.md). The replicated Stripe case also
passed a [three-attempt screen](stripe-config-screen.md). The two single-tier
cases have no Codex difficulty scores. A separate
[notification-recovery variant](gitlab-notification-recovery.md) extends the
replicated GitLab incident with an independently durable mail queue.

| Case | PostgreSQL members | Original PVCs | Reconciled tail entries | Admission + restart | Total including setup/cleanup |
|---|---:|---:|---:|---|---:|
| gitlab-single | 1 | 5 | 6 | Pass | 1387s |
| gitlab-replicated | 3 | 7 | 30 | Pass | 1148s |
| stripe-single | 1 | 4 | 6 | Pass | 339s |
| stripe-replicated | 3 | 6 | 24 | Pass | 509s |

GitLab admission rejects deleted schemas, the truncated advertised backup,
availability restored from an older backup with missing issues, and partial
journal replay. Complete replay preserves public issue identity, user/project
access settings and Git files; a second replay creates no duplicates. The final
checks also pass after restarting GitLab.

Stripe admission rejects oversized configuration, a currently healthy file whose
producer can publish a bad successor, containment with undelivered events, and
partial webhook replay. Full recovery delivers original events without changing
financial records. A scoped metadata query supports continued generation after
restart. Successful recovery includes 42 seconds of traffic sampling. Its grader
also rejects missing payment state and remaining work for acknowledged events.

The targeted unit/regression suite passed **49 tests**. Stripe's ordinary
conductor validation covers setup, healthy grading, injection, fault detection,
reference recovery, recovered grading and teardown; see the machine-readable
stage results for the exact eight-stage breakdown.

The first GitLab development attempt exposed PostgreSQL's default lock-table
limit. The recovery environment now provisions 1,024 locks per transaction for
atomic operations across GitLab's large schema. An initial Stripe startup exposed
a ClickHouse HTTP authentication-header mismatch, which was corrected before
clean admission. An earlier GitLab restart check overlapped the conductor’s worker-kubelet resets
and returned an ambiguous command failure. That run is also excluded. Final
admissions ran after the conductor finished; these workflows require exclusive
cluster access. During fresh setup, an unused GitLab image cached on another
KIND worker was removed when its duplicate pull filled the private Docker disk;
readiness checks kept validation from starting early. Development artifacts are retained and excluded from the table.

All runs use the original Stripe reference/adapter image without changing its
upstream business implementation. The edge, producer and replay tool are separate
incident runtime components. Existing prototype contract results remain in the
[prototype report](saas-prototype-results.md).

See [task design and reproduction](saas-postmortems.md) for problem IDs, commands,
historical sources, and fidelity limits. These mocks compress time and use one
physical host. GitLab uses logical schema loss and a synthetic recoverable issue
journal; Stripe uses one catalog with two permission identities and one edge.
The traces establish the tested recovery behavior, not production-scale fidelity
or a statistical reliability estimate.

[Full case results and runtime image IDs](saas-postmortem-results.json).
Raw logs and additional development evidence are under
`results/dind/sregym-postmortems/` in this checkout. The final local reproduction
image is `sregym-dind:postmortems`; its build manifest and smoke-test evidence are
stored alongside those logs. Code and reports remain uncommitted and unpushed.

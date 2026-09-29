# Stripe recurring configuration: Codex screen

All **3/3 fresh attempts passed** `stripe_feature_config_replicated`. This
candidate does not meet the requested target of at least one genuine agent
failure in three valid attempts. Its source and grader stayed unchanged during
the screen.

| Attempt | Agent time | Grading time | Result |
|---|---:|---:|---|
| 1 | 110.8 s | 86.2 s | Pass |
| 2 | 110.3 s | 87.3 s | Pass |
| 3 | 133.8 s | 86.6 s | Pass |

Every trace records `gpt-6-astra` and Codex CLI `0.157.1`. Runs used agent-default
reasoning, mitigation only, the `svelte` profile and a 900-second solving budget
inside the same local DinD environment as the GitLab screens. All three attempts
completed and cleaned up successfully; none was ambiguous or invalid.

Each repair preserved 24 acknowledged payments and their original webhook
deliveries, three PostgreSQL members, and six original volumes. All agents left
configuration publishing enabled with a scoped metadata query: both rollout
identities generated 120 valid features. Traffic passed the grader's 42-second
observation window. The agents used both the supplied replay tool and direct
transactional queue repair, demonstrating that the grader accepts different
correct recovery methods.

A fresh ordinary conductor lifecycle passed all eight stages in 427.33 seconds
before model evaluation. The injected fault failed with
`recurring_configuration_risk`; reference recovery and teardown passed. Earlier
admission also checked partial recovery and recurrence; see the
[postmortem validation report](saas-postmortem-results.md).

The initial [GitLab deletion screen](gitlab-recovery-baseline.md) and the
[GitLab notification screen](gitlab-notification-recovery.md) also passed 3/3.
These are exploratory calibration results. They do not establish a general
model reliability estimate or demonstrate that the environments became harder.
All results are retained; later candidates must be reported separately.

[Detailed grades, versions and trace hashes](stripe-config-screen.json).
The frozen source, image preparation, lifecycle output and campaign logs are
under `results/dind/sregym-difficulty-baseline/stripe-three/`. Ordinary benchmark
traces are under `0926_0939/codex/stripe_feature_config_replicated/` in that
artifact root. Nothing has been committed or pushed.

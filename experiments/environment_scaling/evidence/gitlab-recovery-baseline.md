# GitLab recovery: initial Codex screen

All **3/3 fresh attempts passed** `gitlab_database_deletion_replicated`. This initial incident does not meet the difficult-problem target. No application or grader changes were made during the screen.

| Attempt | Agent time | Grading time | Result |
|---|---:|---:|---|
| 1 | 437.5 s | 21.2 s | pass |
| 2 | 420.7 s | 21.6 s | pass |
| 3 | 281.5 s | 20.4 s | pass |

Every grade verified 93 original issues on all three PostgreSQL members, six users, six private projects, 11 memberships, Git files, seven original volumes and all 30 acknowledged issue receipts. No missing/changed original issue or duplicate replay was found. One synchronous standby remained required.

The ordinary conductor lifecycle passed all eight stages: deployment, healthy grading, injection, fault detection, reference recovery, recovered grading and cleanup, plus registry resolution. The lifecycle took 1,522.47 seconds.

The model was `gpt-6-astra`, with agent-default reasoning, mitigation only, a 900-second agent budget and the `svelte` profile. The private DinD environment had an eight-CPU quota, 36 GiB memory limit and a 28 GiB memory-backed Docker filesystem. Fresh deployment time is excluded from agent time.

The requested CLI pin was `0.157.0`, but all three trace records and the prepared installation showed **0.157.1**. The frozen `main.py` explicitly read its local registry and ignored `SREGYM_AGENT_REGISTRY`. The checkout now uses the override for both tool preparation and attempt startup; 13 targeted tests pass. Future comparisons must pin **0.157.1** to preserve the actual baseline runtime.

The first two agents restored the database, replayed issues through GitLab's Rails service, and restarted the application services. The third restored the archive and replayed through the API with explicit public issue numbers. Each verified application data before submission.

Detailed grades, timings, runtime versions and trace hashes are in [the result record](gitlab-recovery-baseline.json). Raw local evidence is under `results/dind/sregym-difficulty-baseline/`, including the immutable initial source archive and the runtime-version audit.

Three trials are an exploratory screen. They establish the observed result for this model and configuration, not a universal success rate or a claim that added replicas alone caused difficulty.

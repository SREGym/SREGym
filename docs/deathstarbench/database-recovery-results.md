# Gitea database recovery evaluation

Both lifecycle validations and both live negative-control validations passed.
All **6/6 fresh Codex attempts passed**, three on each tier. The
[machine-readable results](database-recovery-results.json) include per-attempt
metrics, recovery counts, CLI versions, and raw evidence paths. This evaluates the
[database recovery incident](database-recovery.md); the earlier
[selector-fault comparison](results.md) remains a separate experiment.

## Recovery validation

| Check | Single | Replicated |
|---|---:|---:|
| Healthy application passes | pass | pass |
| Deletion reaches all database members and fails grading | pass | pass |
| Truncated recent archive rejected | pass | pass |
| Missing acknowledged issues after older-backup restore | 6 | 30 |
| Missing issues after partial replay | 3 | 15 |
| Full replay restores all required records | pass | pass |
| Second replay creates no duplicates | pass | pass |
| Restart retains recovered state and original volumes | pass | pass |

The final negative-control checks verified 26 original issues on the single
member and 98 original issues on each of the three replicated members, alongside
seven user/organization records, five repositories, and pre-deletion Git files.
These issue counts include the validator's additional healthy workflow probe;
agent attempts have their own freshly captured baseline. The older archive
restored baseline API data before the incident grader confirmed the missing tail.

Raw verdicts:
[single](../../results/dind/sregym-gitea-recovery/recovery-negative-controls-single.json),
[replicated](../../results/dind/sregym-gitea-recovery/recovery-negative-controls-replicated.json).

## Codex comparison

<!-- recovery-comparison-start -->
| Tier | Lifecycle | Agent passes | Median agent time | Median oracle time |
|---|---|---|---|---|
| single | pass | 3/3 | 107.2 s | 20.2 s |
| replicated | pass | 3/3 | 137.2 s | 31.7 s |
<!-- recovery-comparison-end -->

Both tiers have **0% observed difficulty** under the pass-rate definition below.
The replicated tier's median agent time was 137.2 seconds versus 107.2 seconds
for single, but three attempts per tier cannot establish a reliable timing
difference. All six grades verified the required member count, all six or thirty
journal entries, and zero acknowledged data loss. The negative controls show that
the grader rejects incomplete recovery; this model nevertheless completed every
trial. Added state and replicas did not separate pass rates in this sample.

Agent time is `phase.stage:mitigation.duration_s`; grading time is
`phase.evaluate:mitigation.duration_s`. Grading performs more reads on the
replicated tier, so its duration is reported separately. Difficulty is
`1 − mitigation pass rate`, using the runner's explicit infrastructure and
ambiguous-failure exclusions. Three attempts per tier are exploratory.

## Conditions and evidence

- Configured model `gpt-6-astra`, agent-default reasoning, mitigation only,
  900-second agent timeout, three fresh attempts per tier, `svelte` profile.
- All six traces recorded Codex CLI 0.157.0. After the first completed attempt,
  the runtime registry was pinned to that version for subsequent main processes;
  the first process already cached
  that CLI for its three attempts. The earlier selector campaign used 0.156.1,
  adding another difference to comparisons across incidents. The host's registry
  was unchanged. [Version-pin audit](../../results/dind/sregym-gitea-recovery/runtime-version-pin.json).
- A fresh private DinD cluster: four KIND nodes on Kubernetes 1.32.11, 8 CPUs,
  32 GiB memory, and a 24 GiB ext4 Docker filesystem backed by tmpfs.
- The pinned CloudNativePG operator was installed before baseline capture.
  Negative controls and the campaign ran serially. Model attempts are gated on
  each problem's healthy → fault detected → reference recovery lifecycle.
- Lifecycle gates omit Loki; agent attempts include it. Both use the same
  deployment profile and incident implementation.
- The image contains the checkout's stage-aware Codex client. Source files were
  hashed, compared against the running environment, and archived before the
  campaign. [Evaluated source manifest](../../results/dind/sregym-gitea-recovery/evaluated-source-manifest.json).
- Focused unit checks passed: 34 tests in the combined run, 12 oracle tests after
  two additions, and seven comparison-runner tests after adding the version pin
  (37 unique tests across the recorded runs).

The single negative-control run preceded the addition of the oracle's bounded
standby catch-up wait. That change has no effect on a one-member database. Both
campaign tiers use the same final oracle, and the replicated negative controls
use that version. The earlier single-run source hashes are retained separately.
The comparison script gained `--agent-version` after the campaign started to
make this pin explicit in future runs. The running campaign uses its archived
  script plus the recorded runtime registry override; application and oracle code
remain unchanged.

The campaign took 69.6 minutes on this workstation, including its two lifecycle
validations, setup, grading, and reset; the earlier negative controls are separate.
The final cluster snapshot contained no application namespace or persistent
volumes. The private DinD environment was stopped and removed, and temporary
host inotify settings were restored. Raw logs, source archives, application image
identities, and the verified `sregym-dind:recovery` reproduction image are retained.

The tier comparison varies database membership and record volume together.
Comparison with the earlier selector fault also changes the incident and grading
contract. Neither comparison isolates a causal effect of replicas alone. This
remains a bounded logical-recovery fixture on one physical host, with the fidelity
limits described in the [incident guide](database-recovery.md).

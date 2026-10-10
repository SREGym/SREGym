# Validation and Codex comparison

This report covers the original selector fault. The subsequent stateful incident
has a separate [database recovery evaluation](database-recovery-results.md).

All three application campaigns are complete: ten lifecycle checks, three
storage recovery checks, and **30/30 fresh Codex attempts passed**. The
[machine-readable results](evaluation-results.json) retain per-attempt metrics,
validation outcomes, and paths to the raw evidence. Full lifecycle verdicts,
CSVs, traces, and phase timings are retained under `results/dind/` on this
workstation.

Each tier received a separate healthy → injected fault detected → recovered
validation, followed by three fresh Codex attempts. Every attempt deployed new
application state and performed cleanup. The fault remains the existing wrong
service selector; these are application-foundation tests, not historical
postmortem reconstructions.

<!-- comparison-table-start -->
| Application | Tier | Lifecycle | Agent passes | Median agent time | Median oracle time |
|---|---|---|---|---|---|
| HotelReservation | legacy | pass | 3/3 | 82.0 s | 4.4 s |
| HotelReservation | single | pass | 3/3 | 103.4 s | 13.7 s |
| HotelReservation | replicated | pass | 3/3 | 125.7 s | 20.6 s |
| HotelReservation | expanded | pass | 3/3 | 113.3 s | 28.2 s |
| SocialNetwork | legacy | pass | 3/3 | 93.2 s | 4.5 s |
| SocialNetwork | single | pass | 3/3 | 90.1 s | 13.9 s |
| SocialNetwork | replicated | pass | 3/3 | 85.6 s | 20.0 s |
| SocialNetwork | expanded | pass | 3/3 | 101.3 s | 27.6 s |
| Gitea | single | pass | 3/3 | 59.4 s | 14.5 s |
| Gitea | replicated | pass | 3/3 | 59.7 s | 17.7 s |
<!-- comparison-table-end -->

Agent time is the recorded `phase.stage:mitigation.duration_s`; oracle time is
`phase.evaluate:mitigation.duration_s`. The latter grows with the number of
database members checked and should not be interpreted as agent difficulty.
Difficulty is `1 − mitigation pass rate`. Three attempts provide an exploratory
screen, not a reliable ranking or a causal estimate of a timing difference.

Every tier passed 3/3 fresh attempts. There is no observed
increase in pass-rate difficulty for this fault. Hotel's scaled tiers have higher
median agent times than legacy; SocialNetwork's single and replicated tiers have
lower medians, while expanded has a higher median. Neither application shows a
monotonic timing increase. Gitea's medians were 59.4 seconds with one PostgreSQL
member and 59.7 seconds with three. Three runs per tier are insufficient for a
timing conclusion.

## Storage recovery

HotelReservation's replicated storage test passed a real MongoDB primary
election, client recovery, and replacement of the former primary pod. The
original volume identities and an all-member-acknowledged record survived.
[Raw verdict](../../results/dind/sregym-dsb2/hotel-storage.json)

SocialNetwork also passed the same election, client recovery, retained-volume,
and former-primary replacement test after the probe corrections described below.
[Raw verdict](../../results/dind/sregym-dsb2/social-storage.json)

Gitea's storage test passed a controlled PostgreSQL switchover from member 1 to
member 2, replacement of the former primary pod, and a Gitea restart. The complete
oracle rechecked retained issues, repository files, replicas, and original volume
identities after each step. This does not test unplanned node failure or backup
restoration. [Raw verdict](../../results/dind/sregym-gitea/gitea-storage.json)

Gitea preflight caught a seed request missing explicit team unit permissions.
Reviewing the pinned application's model also corrected the replica check to
query `issue.name`, which stores API issue titles. The oracle unit test now runs
that query against a minimal schema instead of accepting arbitrary SQL through
a mock. These fixes precede all Gitea agent attempts.
The configuration check also accepts CloudNativePG's admission-defaulted
`failoverQuorum: false` as equivalent to an omitted value; it still rejects
changes to the required synchronous replication configuration.

The first Gitea harness launch also exposed missing application metadata fields
in `/get_app`. The driver exited before invoking Codex, so it consumed no model
attempt. The application now supplies its name and tier description; a test calls
the actual conductor endpoint. That launch and its logs are retained separately
under `gitea.initial-metadata-failure/`, and the comparison was restarted with
three fresh model attempts per tier.

## Conditions and provenance

- Model: configured default `gpt-6-astra`; reasoning uses the agent default.
- Mitigation-only stage, `svelte` deployment profile, 900-second agent timeout,
  and three attempts per tier. All 30 traces record Codex CLI 0.156.1 and
  `gpt-6-astra`; each run retains its own trace metadata.
- Lifecycle gates use the validator's default without Loki; agent attempts
  include Loki. Both use the `svelte` profile.
- The local client image includes the checkout's stage-aware Codex driver.
  Campaign reports retain its image ID and driver SHA-256.
- DeathStarBench ran serially in one private DinD cluster with four KIND
  nodes, Kubernetes 1.32.11, an 8-CPU limit, a 36-GiB memory limit, and a
  32-GiB ext4 Docker disk backed by tmpfs. Physical failure domains are shared;
  this storage mode does not validate disk performance or host-reboot durability.
- Source hashes and the initial cluster baseline are retained in the
  [corrected run manifest](../../results/dind/sregym-dsb2/corrected-run-manifest.json).
  Gitea ran in a fresh dedicated cluster with its operator installed before
  baseline capture, an 8-CPU limit, a 32-GiB memory limit, and a 24-GiB private
  Docker disk backed by tmpfs. Its [source manifest](../../results/dind/sregym-gitea/gitea-source-manifest.json)
  records the evaluated application and oracle hashes.

Before SocialNetwork's live validation, its scaled adapter corrected a stale
media-frontend container port: the chart declares 8081 while its Service and
Nginx configuration use 8080. A rendered-manifest test checks agreement between
the Service, listener, and readiness probe. This change is confined to the
SocialNetwork adapter; Hotel's implementation is unchanged. The
[SocialNetwork source manifest](../../results/dind/sregym-dsb2/social-source-manifest.json)
records the updated hashes.

SocialNetwork's first storage preflight then exposed a probe bug: login sets its
cookie on a redirect, while the probe read only the final page's headers. The
fixed probe reads the cookie jar. A subsequent healthy posting check found the
upstream empty-recipient Redis error; the probe now creates a follower before
posting. The seeded ring was independently read from MongoDB: all 962 seed users
had two followers and two followees. The repaired business workflow and the
fresh storage validation both passed. No SocialNetwork agent
attempts were spent before these fixes. Failed preflight and debug logs remain
under `results/dind/sregym-dsb2/`.

The earlier Hotel pilot is excluded because its released driver instructed
diagnosis first during mitigation-only attempts. The corrected campaign uses
three fresh attempts per tier, as requested. The pilot also exposed a volume
cleanup race; application cleanup now waits for provisioner reclamation before
cluster reconciliation. A separate corrected preflight failed Helm setup before
any agent ran; it is retained as infrastructure evidence, not a model failure.

During the corrected legacy tier, unused tmpfs-backed disk blocks were reclaimed
and automatic discard was enabled. Later, unused legacy MongoDB, Hotel application,
Consul, and Hotel Memcached images were removed from the private nodes to preserve
disk headroom. These maintenance events are recorded with the results; timings
are descriptive rather than controlled performance measurements. The expanded
SocialNetwork attempts ran with roughly 700–800 MiB of free private disk space;
no corrected agent attempt failed for lack of disk space. All application volumes
were reclaimed at the end of both the DeathStarBench and Gitea campaigns.

Both private DinD environments were stopped and removed after evaluation, and
the temporary host inotify settings were restored to their original values.
Final cluster snapshots record zero remaining persistent volumes. The local
`sregym-dind:dsb2` image packages the final source as a verified overlay on the
full DinD image built earlier in this session; its packaging manifest is separate
from the evaluated source manifests.

Legacy applications retain their original, weaker oracles. The three new
DeathStarBench tiers share a stronger stateful oracle and vary application
replicas and workload as well as database membership. Both legacy applications
use their original 100 requests/s workload default; the new tiers use 10, 20,
and 30 requests/s. SocialNetwork's new tiers
also share seeded users, follows, and posts that its legacy setup does not add.
Gitea keeps one application server in both tiers and varies PostgreSQL members.
See the [implementation guide](README.md) and [Gitea guide](gitea.md) for the
remaining fidelity boundaries.

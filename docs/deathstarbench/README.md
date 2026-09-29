# DeathStarBench 2.0: replicated application tiers

This first implementation scales **both existing applications**, HotelReservation
and SocialNetwork. It keeps their service binaries and business APIs, replaces
each standalone MongoDB Deployment with a real replica set, and increases the
number of application-service replicas. It is an environment foundation for
postmortem families, not a claim to reproduce a particular historical incident.

For additional applications, see the [SWE-Marathon and open-source reuse assessment](application-reuse.md),
including specific passing clone trials, reusable benchmark assets, and a
Gitea/GitLab adoption sequence. The first reusable SaaS pilot is
[Gitea with PostgreSQL replicas](gitea.md).
The [validation and comparison report](results.md) records live experiment
results and their limitations.
The next incident family exercises [Gitea database deletion and recovery](database-recovery.md),
including backup validation and reconciliation of acknowledged post-backup writes.
Additional [GitLab CE, Mattermost and SWE-Marathon Stripe prototypes](saas-prototypes.md)
provide PostgreSQL-backed application tiers and business-state admission checks.
The [GitLab and Stripe postmortem families](saas-postmortems.md) add wrong-primary
database deletion and recurring bad-configuration rollout, with recovery-tail
graders and two PostgreSQL tiers each.
The [GitLab notification-recovery variant](gitlab-notification-recovery.md)
adds a persistent Sidekiq queue, real SMTP delivery, and checks for lost,
duplicate, or wrong-recipient notifications after database restoration.
The [difficulty calibration report](difficulty-calibration.md) records the fixed
three-attempt postmortem screens and the current candidate's validation status.

This workstream now covers **six application families**: the two existing
DeathStarBench applications above, plus **four new applications**—Gitea, GitLab CE,
Mattermost and SWE-Marathon Stripe. Each new application has `single` and
`replicated` PostgreSQL tiers. Additional scale tiers and incident scenarios do
not count as additional applications. Slack, Mastodon and S3 clones remain
research candidates in the reuse assessment.

The following MongoDB tiers apply to HotelReservation and SocialNetwork:

| Tier | Application replicas per service | Members per database | Total MongoDB pods per app | Workload requests/s |
|---|---:|---:|---:|---:|
| `single` | 1 | 1 | 6 | 10 |
| `replicated` | 2 | 3 | 18 | 20 |
| `expanded` | 3 | 5 | 30 | 30 |

All members store data; none are arbiters. Each has a stable hostname, its own
2 GiB dynamically provisioned PVC, a bounded WiredTiger cache, and a disruption
budget preserving a majority. Placement preferences distribute members across
KIND workers without requiring five physical nodes. These are logical failure
domains on one machine; they do not emulate independent regions.

The one-member tier shares the replication protocol and stateful oracle with
the larger tiers. Original applications and problem IDs remain available as
legacy comparisons. Both legacy applications retain their original 100 requests/s
workload default; the new tiers use the smaller per-tier rates above. Legacy
comparisons therefore change workload as well as storage, replicas, and grading.
All six experimental IDs are explicit opt-ins:

```text
wrong_service_selector_hotel_reservation_single
wrong_service_selector_hotel_reservation_replicated
wrong_service_selector_hotel_reservation_expanded
wrong_service_selector_social_network_single
wrong_service_selector_social_network_replicated
wrong_service_selector_social_network_expanded
```

The existing `WrongServiceSelector` problem accepts `scale_tier`. For another
problem, instantiate `ScaledHotelReservation(tier="replicated")` or
`ScaledSocialNetwork(tier="replicated")` as its application. Faults targeting
database **Deployments** need adaptation to StatefulSets; they cannot be assumed
to work unchanged. Application-service selectors and names are preserved.

## Lifecycle and outcome checks

Deployment renders the existing manifests into memory, creates storage and
replica sets, waits for elections, starts one service replica to avoid concurrent
initial-data seeding, then scales services out. It does not force-reconfigure
an existing replica set. SocialNetwork seeds the 962 users expected by its
upstream mixed workload, a deterministic graph with two follows per user, and
one initial post per user through the real APIs. A setup marker prevents normal
redeployment from reseeding incident state. This seeded state is shared across
its three new tiers; the original legacy application keeps its original setup.
MongoDB 4.4.29 is pinned for compatibility with the
HotelReservation mgo client's legacy wire protocol; modernizing those clients
and database versions is separate work. HotelReservation's existing geo/rate
authentication is retained, including its benchmark credentials. SocialNetwork
retains its existing unauthenticated database access.

The lifecycle validator requires **healthy → injected fault detected → recovery
healthy**, and attempts cleanup after failures. The scaled mitigation oracle
adds the following checks to the original service-endpoint oracle:

- All baseline application deployments retain their required replicas.
- Every expected database volume remains bound with its original identity.
- Every replica set has its configured member count and one primary, with all
  members healthy as primary or secondary.
- A pre-fault durability canary survives on every member; a fresh write receives
  acknowledgement from every member and can be read back from each one.
- Hotel search returns inventory and a new reservation reaches MongoDB, or a
  SocialNetwork user can register, log in, gain a follower, compose a post, and read it on their
  timeline, with the post also verified in MongoDB.

The SocialNetwork probe retains the login cookie across the application's HTTP
redirect and connects its new account to a seeded follower before posting.
The upstream home-timeline path returned a Redis error for a post with no
recipients during validation; the connected-user workflow exercises its normal
fanout path without changing the upstream binaries.

The canary detects loss of the seeded durability record, not arbitrary corruption
of every business record. Future deletion/divergence tasks need incident-specific
data-loss accounting, backup validation, and reconciliation graders. Persistent
data and real asynchronous replication support those extensions. Consul, Redis,
and Memcached retain their upstream topology in this version. There are no
multi-region links, sharding, backup/restore policies, tool degradation, or
postmortem-specific recovery automation yet.

## Run on one machine with DinD

Build from the repository root, then validate one tier:

```bash
python3 docker/dind/run.py build
python3 docker/dind/run.py run --name dsb2-validate --memory 32g -- \
  python tests/integration/validate_problem.py \
    --problem wrong_service_selector_hotel_reservation_replicated \
    --profile svelte --summary results/validation.md \
    --json-summary results/validation.json
```

KIND supplies the default `standard` StorageClass. On another cluster, pass a
supported `storage_class` to the scaled application's constructor. Normal
namespace cleanup lets the provisioner reclaim application volumes; it does not
remove unrelated volumes or strip their finalizers.

Independently verify a real primary election, client recovery, and data surviving
pod replacement on either application's three-member tier:

```bash
python3 docker/dind/run.py run --name dsb2-storage --memory 32g -- \
  python tests/integration/validate_deathstarbench_storage.py \
    --application hotel_reservation --output results/storage-validation.json
```

The storage test creates and cleans up its own application namespace. Run it
separately from an active benchmark attempt.

Run the matched campaign with an explicit agent, model and three attempts per
tier. `--agent` selects the client; `codex` and `claudecode` are supported:

```bash
# Claude Code, subscription credentials
python3 docker/dind/run.py run --name dsb2-comparison --memory 36g \
  --claude-auth-file "$HOME/.claude/.credentials.json" -- \
  python scripts/evaluate_deathstarbench.py --agent claudecode \
    --model claude-opus-4-8 --attempts 3 --profile svelte

# Codex, subscription credentials
python3 docker/dind/run.py run --name dsb2-comparison --memory 36g \
  --codex-auth-file "$HOME/.codex/auth.json" -- \
  python scripts/evaluate_deathstarbench.py --agent codex \
    --model gpt-6-astra --attempts 3 --profile svelte
```

Alternatively supply `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` or
`CLAUDE_CODE_OAUTH_TOKEN` through the existing launcher environment forwarding.
The `--*-auth-file` options mount only that one file read-only; agent containers
receive the harness's own isolated copy, so a token refresh inside a container
cannot rewrite host credentials. The campaign uses mitigation only; add diagnosis
evaluation separately when a diagnosis judge is required.

**Attempts from different agents are not one cohort.** Changing `--agent` or
`--model` starts a new comparison and does not extend an existing one.

The campaign builds a small local agent image from the pinned released runtime
and this checkout's client and helper modules for the selected agent. Both
drivers read the conductor's active stage, so mitigation-only attempts receive
repair instructions rather than a diagnosis-first prompt that would submit before
any repair. This avoids silently using an older driver baked into a released
image. The report records the agent, image ID and driver hash; `--agent-image`
allows an explicit override. Direct `main.py` runs can select that image with the
same option or rebuild the complete agent image using `--force-build`.
Use `--agent-version VERSION` on the comparison script to pin the
runtime-installed agent CLI through a private registry copy. A pinned base image
alone does not pin that installation; the original registry remains unchanged.

The campaign validates each problem before running its agent attempts. It writes
validation logs, JSON verdicts, `comparison.json`, and `comparison.md` under
`results/deathstarbench/`. Ordinary SREGym CSVs, phase ledgers, and agent traces
remain under `results/`. The outer launcher retains these files under
`results/dind/<run-name>/`. Use `--validate-only`, `--applications`, or `--tiers`
to select a smaller campaign. Run one campaign at a time in each outer container.

Plan for 8 CPUs, 32–36 GiB RAM for the larger tiers, and substantial Docker image
and volume storage. These are starting budgets, not measured minimums. The
existing `--docker-tmpfs-size` option can help a disk-constrained workstation,
but changes storage behavior and consumes the outer container's memory budget.
It is unsuitable for evaluating disk performance or host-reboot durability.
Use the same storage mode, deployment profile, model, reasoning setting, and
timeout across every comparison.

Difficulty is reported as `1 − mitigation pass rate` over complete attempts.
Missing grades, infrastructure failures, and unattributed failures make a requested campaign
inconclusive; they are not model failures. Three attempts are an exploratory
screen. Added infrastructure does not itself establish increased difficulty,
and the legacy oracle is weaker than the shared oracle used for all three new
tiers. Postmortem families should hold their causal fault constant while varying
topology, state size, workload, evidence, and recovery tail deliberately.

Implementation references: [MongoDB replica-set bootstrap and keyfile authentication](https://www.mongodb.com/docs/v7.0/tutorial/deploy-replica-set-with-keyfile-access-control/),
[MongoDB localhost bootstrap exception](https://www.mongodb.com/docs/manual/core/localhost-exception/),
and [Kubernetes StatefulSets and persistent identities](https://kubernetes.io/docs/concepts/workloads/controllers/statefulset/).

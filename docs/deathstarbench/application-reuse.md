# Reusing applications for environment scaling

Research date: 2026-09-24. This assessment led to the
[Gitea pilot](gitea.md), which imports ZOO fixtures and adds persistent
PostgreSQL tiers. Follow-up implementation now includes
[GitLab CE, Mattermost and the Stripe reference application](saas-prototypes.md).
Other candidates below remain research only. The DeathStarBench scaling implementation is described in
[README.md](README.md).

The strongest next foundation is **Gitea with PostgreSQL replication**, followed
by **GitLab Community Edition for the database-loss family**. Reuse other
benchmarks' seed data, workflows, and state assertions around those real
applications. SWE-Marathon also supplies useful compact reference applications
and successful construction traces, but its clones still need substantial
storage and process separation before they exercise production failure modes.

## What succeeded in SWE-Marathon?

The current site's published data contains these specific binary-passing runs.
I fetched each linked trajectory and recorded its SHA-256, the reported scores,
and the site's data-bundle SHA-256 in [reuse-evidence.json](reuse-evidence.json).
These are publisher-reported results, not independently reproduced grades.

| Task | Passing trial | Agent/model | Reported checks | Public artifact |
|---|---|---|---|---|
| Mastodon clone | `mastodon-clone-1192` | Codex / GPT-5.6-sol, xhigh | Reward 1; 19/19 correctness gates; UX 1 | [Trajectory](https://www.swe-marathon.org/trajectories/v1.1/mastodon-clone-1192.json) |
| Stripe clone | `stripe-clone-glm53-high-01` | Claude Code / GLM 5.3, high | Reward 1; 12/12 correctness gates | [Trajectory](https://www.swe-marathon.org/trajectories/v1.1/stripe-clone-glm53-high-01.json) |
| Excel clone | `excel-clone-1625` | Codex / GPT-6 Astra, max | Reward 1; 18/18 correctness gates; UX 1 | [Trajectory](https://www.swe-marathon.org/trajectories/v1.1/excel-clone-1625.json) |

Source: [SWE-Marathon task browser](https://www.swe-marathon.org/#tasks), including
its [published data bundle](https://www.swe-marathon.org/assets/index-Cnn3YyxR.js).
The inspected Slack and S3 clone rows did not contain a binary pass. This does
not establish that no successful implementations exist elsewhere. A rollout's
`status: success` means it ran successfully; it can still have `reward: 0`.
Partial scores are not task completion.

The trajectories contain file writes and edits, but I did not find a linked,
complete final source archive for these selected runs. Replaying arbitrary
trajectory commands is not a reliable build recipe: they include exploratory
commands, temporary files, and modifications made through different tools.
The repository separately publishes **human-written reference solutions**;
those are not the agents' passing submissions. Its log-download instructions
describe a roughly 800 GB bucket and require credentials from the authors.
The small trajectory files above are publicly accessible without those
credentials. [Repository and artifact instructions](https://github.com/abundant-ai/swe-marathon)

### Which parts are useful?

| Candidate | Immediately reusable source | Useful SRE semantics | Required adaptation |
|---|---|---|---|
| Slack clone | [Reference solution](https://github.com/abundant-ai/swe-marathon/tree/5c468fae8656ef9f7bca36cc8c6ee6e7478aa0f6/tasks/slack-clone/solution), protocol/crash tests | Ordered message streams, reconnect/replay, Redis outage, independent HTTP process crashes | Three processes currently share one SQLite file and one container. Externalize the database and Redis; isolate processes before claiming distributed failure coverage. |
| Mastodon clone | [Reference solution](https://github.com/abundant-ai/swe-marathon/tree/5c468fae8656ef9f7bca36cc8c6ee6e7478aa0f6/tasks/mastodon-clone/solution), API/UX checks | Persistent social data, authentication, media, background processing | Reference storage hardcodes SQLite. A PostgreSQL port needs fresh concurrency, durability, and migration validation. |
| Stripe clone | [Reference solution](https://github.com/abundant-ai/swe-marathon/tree/5c468fae8656ef9f7bca36cc8c6ee6e7478aa0f6/tasks/stripe-clone/solution), SDK-driven tests | Idempotency, state transitions, delayed webhooks and retry backlog | Separate API, workers, and replicated storage; validate no duplicate effects during recovery. A payments API is not itself a Knight Capital trading simulator. |
| S3 clone | [Reference solution](https://github.com/abundant-ai/swe-marathon/tree/5c468fae8656ef9f7bca36cc8c6ee6e7478aa0f6/tasks/s3-clone/solution), boto3 checks | Object integrity, multipart uploads, tenant boundaries | Useful API surface; a single-container implementation does not reproduce S3's distributed metadata recovery. |
| Excel clone | Passing trace above | Durable documents and concurrent edits | Lower priority for the proposed infrastructure incidents. |

The benchmark repository is Apache-2.0. Preserve its notices and inspect bundled
dependencies when importing a reference solution; don't assume that this also
establishes the licensing or completeness of an external agent snapshot.
[License](https://github.com/abundant-ai/swe-marathon/blob/5c468fae8656ef9f7bca36cc8c6ee6e7478aa0f6/LICENSE)

## Other benchmarks with reusable assets

These projects solve different parts of the problem. Browser-task completion
rates measure agents *using* applications and should not be presented as
evidence that agents built those applications.

| Project | Assets inspected | Best use in SREGym | Qualification |
|---|---|---|---|
| [ZOO / the_zoo](https://github.com/bgrins/the_zoo) | Real Gitea and Mattermost, shared PostgreSQL, Redis, mail/OIDC, Git repository fixtures, SQL seeds, snapshot tests | First source for a smaller self-hosted SaaS environment and repeatable initial state | Database containers reset on restart; replace that behavior with explicit between-attempt reset. The separate `zoo-sites` simulations use in-memory state and are a different substrate. |
| [TheAgentCompany](https://github.com/TheAgentCompany/TheAgentCompany) | GitLab, Plane, ownCloud, Rocket.Chat; populated instances; task initialization and evaluators | GitLab project/issue fixtures, support/chat context, workflow assertions | Whole suite asks for 30+ GB free disk. Import selected assets rather than the full company. Repository MIT license does not replace each application's license. |
| [WebArena](https://github.com/web-arena-x/webarena/blob/main/environment_docker/README.md) | Self-hosted GitLab, shopping and forum environments; reset/deployment instructions | Another source of GitLab/e-commerce fixtures and successful user workflows | Frozen images are useful initial states, not HA topologies. Audit reset/recovery commands before reuse in integrity-graded incidents. |
| [SaaS-Bench — UniPat-AI](https://github.com/UniPat-AI/SaaS-Bench) | 23 self-hosted apps, 106 browser tasks, app registry, Compose templates, state verifiers; includes Mattermost | Broader app catalog, health probes, workload and grader seeds | Full image set requires roughly 100 GB free disk. Download only selected apps; it evaluates SaaS use, not SaaS construction. |
| [SaaSBench — ShadeCloak](https://github.com/ShadeCloak/SaaSBench) | 30 construction tasks, heterogeneous stacks, evaluation harness and task-input download script | Find further specifications and validation suites for agent-built backends | Different project from UniPat's SaaS-Bench. I did not establish a licensed collection of passing final application snapshots in the inspected repository. |
| [RealReplicaBench](https://github.com/Accio-Lab/RealReplicaBench) | Local mock services, task workspaces, graders, execution artifacts | Incident-response surfaces and state-change checks | Mock SaaS behavior must be distinguished from replicated production storage. Code is Apache-2.0; task assets use a separate CC BY 4.0 license. |
| [BackendForge](https://arxiv.org/abs/2607.11042) | Paper describing 56 Dockerized backend-construction tasks with HTTP/OpenAPI checks | Contract-based application admission tests | Useful methodology; this inspection did not establish a downloadable, licensed collection of final passing implementations. Do not list it as an imported application. |

ZOO's **infrastructure repository** currently carries Apache-2.0; the project
website footer labels GPL-3.0. Treat each repository's pinned license as the
artifact-specific evidence, and inspect `zoo-eval` separately if its code is
imported. [Inspected infrastructure license](https://github.com/bgrins/the_zoo/blob/862c02dd73f2101a4b24f168d0fb7fed387dc5f4/LICENSE)

UniPat's SaaS-Bench also separates Apache-2.0 harness code from CC BY 4.0 task
assets, including `verify.py`; preserve task attribution when adapting graders.
[Task-data license](https://github.com/UniPat-AI/SaaS-Bench/blob/48c22deb18b98c0ed78f81e7f3be82bc162de2c8/LICENSE-DATA)

Concrete ZOO paths to start from are
`sites/apps/gitea.zoo/`, `core/postgres/seed/gitea.sql`,
`core/postgres/seed/mattermost.sql`, `tests/sites/gitea.zoo.test.ts`, and
`tests/infrastructure/snapshot-round-trip.test.ts` at revision
`862c02dd73f2101a4b24f168d0fb7fed387dc5f4`. The Gitea Dockerfile pins `1.27.3`.
TheAgentCompany has `servers/gitlab/` and
`workspaces/tasks/pm-copy-plane-issues-to-gitlab/` at revision
`98b68ef82a47690c316f42fddb05baafaab56851`; its GitLab Dockerfile starts from
`gitlab/gitlab-ce:17.5.1-ce.0`. Seed compatibility needs revalidation if that
application version changes.

## Open-source applications and scaling limits

**GitLab CE is open source under MIT.** GitLab EE has a separate, more restrictive
license. Choose CE explicitly when redistributing a benchmark image.
[GitLab licensing](https://docs.gitlab.com/development/licensing/)
GitLab supplies a rich application boundary around PostgreSQL, Redis, background
jobs, and repository storage. It is the closest application match for the 2017
incident, but a modern deployment remains a reconstruction, not the historical
system. The current single-node guidance gives 16 GB as the memory baseline,
with at least 8 GB possible in constrained configurations.
[Installation requirements](https://docs.gitlab.com/install/requirements/)

**Gitea's community code is MIT-licensed.** It is the preferred smaller pilot.
Start with one app instance and replicated PostgreSQL; add application replicas
only after coordinating repository storage, cache/session/queue state, and
indexing. A replicated database alone does not make Git repositories highly
available. Shared local-path RWO volumes in KIND are not a substitute for shared
repository storage across nodes. The current Gitea Enterprise HA guide describes
its own edition; its instructions are not proof that every feature is available
in the community build. [Community license](https://github.com/go-gitea/gitea/blob/main/LICENSE),
[configuration surface](https://docs.gitea.com/administration/config-cheat-sheet/),
[Enterprise HA topology](https://docs.gitea.com/enterprise/zh-cn/24/installation/high-availability/)

**Mattermost is useful as a stateful workload and responder chat system**, but
its supported multi-server HA feature requires an Enterprise license. A first
open benchmark can use one application server with an external PostgreSQL
cluster and test database, attachment, and dependency failures. Don't describe
that as application-tier HA. [Mattermost's HA licensing requirement](https://docs.mattermost.com/administration-guide/manage/admin/installing-license-key)

For Slack-like infrastructure incidents, the workload being chat is only one
part of fidelity. We must separately implement the packet-loss/worker-exhaustion
causal chain, harmful scaling decision, broken access path, and controlled
recovery. Likewise, a GitLab label alone does not implement the deletion incident.

## Concrete adoption sequence

1. **Gitea pilot:** adapt ZOO's users/projects/Git fixtures into an explicit seed
   job. Keep state on persistent volumes through ordinary process/pod restarts.
   Use PostgreSQL primary/replicas with a tested backup and restore path. Start
   with one app server, then introduce coordinated application/storage scaling.
2. **GitLab CE incident family:** port the same database-loss invariants to real
   GitLab, borrowing selected TheAgentCompany/WebArena fixtures. Include primary
   versus replica context, a failed backup path, recovery progress, and explicit
   accounting for acknowledged writes absent from the chosen restore point.
3. **Compact SaaS family:** import a pinned SWE-Marathon reference implementation
   or a complete, licensed passing snapshot. Stripe's durable effects and retry
   tail are particularly useful. Preserve API contracts while separating storage
   and workers; rerun the upstream verifier after every architecture change.
4. **Responder surfaces:** add seeded chat, tickets, mail, and runbooks from the
   reusable environments. Make their shared dependencies fail where the incident
   requires it, and keep expert personas bounded by available evidence.

For each imported app, admission to SREGym requires a pinned source/image and
license record, deterministic seed, workload that proves real state changes,
healthy/fault/recovered oracle checks, restart persistence, and clean reset.
Database recovery grading must check retained acknowledged writes, replica
convergence, and queue drainage rather than just HTTP availability. A passing
construction benchmark is useful prior evidence, not this admission result.

Run these serially inside the existing DinD environment. The host used for the
initial DeathStarBench campaign has 8 CPUs and about 39 GiB RAM, with very little
free root-disk space. It cannot accommodate whole benchmark image collections
alongside the campaign. Record actual per-tier resources and cold-start costs;
several containers on this machine still share one physical failure domain.

Preserve the original DeathStarBench tiers as the controlled comparison. For
every new incident family, hold the causal fault and recovery invariants fixed
across tiers, then vary database membership, dependency depth, workload, evidence
volume, and recovery tail. Three Codex attempts per tier are an exploratory
screen; new infrastructure by itself is not evidence of increased difficulty.

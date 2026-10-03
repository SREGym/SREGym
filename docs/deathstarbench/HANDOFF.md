# Handoff: environment scaling, and the task on CloudLab

Branch `feat/environment-scaling-postmortems`, 26 commits, 209 files, clean
tree, **nothing pushed**. Everything below is reproducible from that branch.

This document is for the agent picking the work up on CloudLab. Read
[the calibration report](difficulty-calibration.md) next; it is the running
record of every graded screen.

---

## The one result that matters

Three families, re-screened with Codex `gpt-6-astra` under task descriptions that
withhold the diagnosis. Nine valid attempts, no ambiguous or environment
failures.

```
mattermost_capacity_cascade_single   ███   3 of 3   median 243s / 900s
gitlab_regional_failover_single      █░░   1 of 3   median 287s / 900s
coordination_collapse_single         ███   3 of 3   median 437s / 2700s
```

`█` solved · `░` not solved. Reproduce with `python scripts/screen_report.py`.

**Only one family separates a frontier agent, and it is the one that requires a
judgement with no safe default.** Failover asks which of two acknowledged write
sets to preserve when neither restore is sufficient; the agent chose wrong twice
in three, both times by failing back — recovering all six orphaned writes and
destroying all six post-promotion writes at the colliding identities.

**The two families that ask for a procedure were solved every time, quickly.**
Cascade asks the agent to distrust a correct-but-misleading metric: found in four
minutes. Coordination asks for a long gated sequence and is the surprise — it was
built specifically to be hard, with a 360-second recovery floor, five phases each
gated on the previous settling, premature action *regressing* progress, tooling
that lies, and permanently accumulating loss. The agent solved it 3 of 3 with
**zero regressions, zero dropped requests, one leader election and all three
members intact every time**, two attempts landing within 80 seconds of the
theoretical minimum. Every protective mechanic went untouched: it read the
refusal messages and waited.

### Two things this overturns

1. **"Scale the horizon" is not the lever.** I previously recommended building
   more long-horizon families (Rogers, Kinesis, CrowdStrike). On this evidence
   duration, damaged tooling and accumulating cost all failed to bite. Build
   incidents around *a decision the responder can get wrong*, which is cheaper
   than a 73-hour recovery and demonstrably works.
2. **Earlier saturation was partly my own doing.** Every pre-correction screen
   ran against descriptions that stated their own diagnosis. The failover family
   scored 3 of 3 because one line I wrote into its evidence said *"dba: do NOT
   restore the east snapshot over the live database, we have taken writes
   since"* — the incident's central decision, handed over. Removing it took the
   family from saturated to 1 of 3. Treat every result dated before 2026-10-02
   as void; they are quarantined under `results/_voided/`.

**Never disclose the diagnosis.** The harness gives a generic "diagnose and fix"
instruction and the application description says what the application *is*, in
the register the stock applications use. No guide files, no README written into a
volume, no colleague chatter authored at the responder. Tests enforce this, since
sanitising a leak only lasts until the next edit. The one exception that had to be
*solved* rather than deleted: the coordinator's operator API is unguessable, so
the service advertises its own routes at `GET /` — discovery, not disclosure.

### The leak was wider than those three families

The three above were cleaned first because they were the ones being screened. On
2026-10-02 the same treatment was applied to every remaining family: six guide
constants (`gitea_recovery`, `gitlab_recovery`, `stripe_config`, and three
notification variants) with the archive writes and the ConfigMap entry that
mounted them, plus five `operations.log` writes. Those logs were the worst of it
— one quoted the destructive statement verbatim (`statement: DROP SCHEMA public
CASCADE`), another named both the count of accepted deliveries and the queue the
unsent mail was sitting in.

I had judged four of those guides harmless on the grounds that everything in them
was discoverable anyway: file inventories by `ls`, response shapes by one request,
tool usage from `--help` or a module docstring. That reasoning is wrong in a way
worth remembering. A guide that buys the agent nothing it could not find still
costs the screen its meaning, because it changes the *task* from "investigate"
to "read and comply" — and the trace shows the agent taking the cheaper path
every time. Discoverable is not the test. Authored is the test.

What stayed, because it is data rather than narration: the acknowledged-write
journal, and the backup catalog whose mismatched checksum *is* the broken-backup
puzzle.

`tests/service/apps/test_coordination_store.py` now enforces this structurally —
no family may define a `GUIDE` constant or write a
README/notifications/provider-audit/failover file — rather than only rejecting
known phrases, which lasts until someone invents a new phrase.

### Clean-description results for the formerly-leaking families

Screened 2026-10-02, Codex `gpt-6-astra`, n=3, same harness and graders as the
three above. Partial — the four notification variants and the stripe re-run were
still running when this was written; `python scripts/screen_report.py
results/clean-screen` has the current table.

```
gitea_database_deletion_single    ███   3 of 3   median 152s / 900s
gitlab_database_deletion_single   ███   3 of 3   median 269s / 900s
stripe_feature_config_single       --   no result: deploy gate failed, see below
```

Both deletion families are saturated *honestly*. I pulled the traces to check,
because a 3-of-3 right after deleting the briefing is exactly what a remaining
leak would look like. They are clean: the prompt is the generic instruction plus a
factual topology sentence, and the agent's first command is `kubectl get
pods,pvc,svc`. It derives the diagnosis itself — Gitea's 500s, then `\dn`/`\dt`
showing no application tables — then reads the backup catalog, compares
checksums, rejects the truncated `latest.dump`, restores the valid archive,
replays the journal, and verifies identities and confidentiality flags before
submitting. GitLab's attempt 1, in its own words:

> The latest backup is truncated and fails its checksum, but the staging backup
> matches its recorded checksum. I'm restoring that backup, then I'll replay the
> six acknowledged issue changes recorded after it.

**So the briefings were inflating these families, and removing them did not make
them hard.** That is not a null result — it localises what was actually missing.
Both families fail the one property that separated failover: **a plausible wrong
action that succeeds silently.** Restoring the truncated dump does not quietly
lose the tail, it errors out of `pg_restore`; the journal states the tail rather
than leaving its absence to be noticed. Every wrong path self-corrects, so the
broken-backup twist reduces to a signposted checksum comparison.

This is direct evidence for the cheaper of the two next-family options: adapt
`gitlab_database_deletion` so the truncated restore *succeeds* and silently drops
the acknowledged tail. Same environment, same graders, and the failure becomes
invisible at the moment of action instead of announcing itself.

### The controlled comparison that explains everything else

```
gitlab_notification_recovery_replicated       ███   3 of 3   median 614s / 900s
gitlab_notification_ambiguity_replicated      ███   3 of 3   median 738s / 900s
gitlab_notification_intermittent_replicated   █░░   1 of 3   median 797s / 900s
gitlab_notification_delayed_audit_replicated  ░×░   0 of 2   median 692s / 900s  (1 timeout)
```

The four are a subclass chain, each adding exactly one impairment to the one
above it, and they produce a monotone ladder with a dose response in the *size*
of the error:

| family | fault during recovery | audit | solved | duplicates |
|---|---|---|---|---|
| recovery | stopped | complete | 3 of 3 | 0 |
| ambiguity | stopped | paginated, digest noise | 3 of 3 | 0 |
| intermittent | **ongoing** | paginated | 1 of 3 | 5, 3 |
| delayed_audit | **ongoing** | **receipts published late** | 0 of 2 | 7, 6 |

Every single failure across both discriminating variants is the same mode:
`expected 30, delivered 30, missing 0, duplicates N`. **The agent never loses
data. It over-sends when it cannot tell what already landed.** That is one
failure mode, graded unambiguously, reproducible, and it scales with how badly
the delivery record lags reality.

`GitLabNotificationIntermittent` subclasses `GitLabNotificationAmbiguity`, and
the whole agent-visible difference between the 3-of-3 family and the 1-of-3
family is one line of `prepare_recovery_material`:

```python
self.app.ongoing_fault(True)
```

Same application, same grader, same budget, same agent. 3 of 3 becomes 1 of 3.
Both failures are the predicted mode, and the grader is unambiguous about it:

```
attempt 2: expected 30, delivered 30, missing 0, duplicates 5
attempt 3: expected 30, delivered 30, missing 0, duplicates 3
```

Nothing lost, everything delivered, some of it twice. The agent retried sends
that had already been accepted, and nothing told it so.

**Why `ambiguity` is solved and `intermittent` is not.** Ambiguity's fault is
*stopped*: the delivery audit is static, merely paginated and padded with
ordinary digest mail, so the agent pages through it, filters the noise and
reconciles exactly once -- it costs 738 of 900 seconds, and it works.
Intermittent's fault is still *running*: the audit trails sends that are failing
while you read it, so no amount of investigation yields a trustworthy answer at
the moment the agent has to act.

### The rule, after five families

> **A family separates agents when the information needed to act correctly does
> not exist at decision time.**

Two shapes produce that, and both are now evidenced:

- a **conflict** -- failover's two acknowledged write sets with colliding IIDs,
  where only one can survive and no query says which;
- a **race** -- intermittent's delivery record trailing the deliveries.

Everything else got solved: laborious, ambiguous-but-static, and loudly-wrong all
fall to more investigation. `ongoing_fault` is therefore the most valuable lever
found so far, and the cheapest -- a flag on a mailbox that already exists, not a
new environment.

### Difficulty is partly hiding in the budget

```
gitea_database_deletion         152s
gitlab_database_deletion        269s
gitlab_notification_recovery    614s
gitlab_notification_ambiguity   738s   (attempts: 840 / 738 / 593)
```

The solved families are not quick, they are *long*, and one attempt used 93% of
the budget. At a 600-second budget `ambiguity` would read about 1 of 3 -- not
because the agent got worse, but because the budget stopped paying for the
labour. **A pass rate at a fixed budget is a statement about the budget as much as
the task.** Record the budget beside every result, and prefer families that fail
on judgement rather than on the clock: the first kind is a property of the task,
the second moves whenever someone changes a timeout.

All of these are n=3 on one agent. One solve moves any of them, and the n=10
confirmation below has not been run.

## What exists

### Six application families, all opt-in

HotelReservation and SocialNetwork gained `single`/`replicated`/`expanded`
MongoDB replica-set tiers. Four new applications — Gitea, GitLab CE, Mattermost,
SWE-Marathon Stripe — each with `single`/`replicated` CloudNativePG tiers.
**31 opt-in problem IDs.** No existing problem ID, application or set membership
changed; `sregym-lite` is untouched.

### Incident families, by recovery shape

| Family | Shape | Status |
|---|---|---|
| Gitea / GitLab database deletion | restore + replay a tail | admitted |
| GitLab notification ×4 | restore + reconcile delivery effects | admitted, 0% |
| Stripe recurring config | stop a recurrence | admitted, 0% |
| GitLab regional failover | **two histories, neither sufficient** | admitted, **0%** |
| Mattermost capacity cascade | **a correct metric that misleads** | admitted, **0%** |
| **Coordination collapse** | **long horizon + broken tools + accruing cost** | **admitted, unscreened** |

### The newest family is the one to care about

`coordination_collapse_{single,replicated}` — modelled on the Roblox 2021 Consul
outage, built specifically because the other two "clever shape" families still
scored 0%. It is the first to use the three levers the SREGym 2.0 proposal lists
for long-horizon work. Full design in
[coordination-collapse.md](coordination-collapse.md).

1. **A recovery floor that cannot be compressed.** Five phases, each gated on the
   previous one *settling*. Measured floor **360 s** (`single`) / **510 s**
   (`replicated`) before any diagnosis. Premature action *regresses* progress
   rather than merely failing. The sharpest mechanic: attempting `compact` early
   restarts the stability window it waits on, so polling to check readiness
   guarantees it never becomes ready — checking is free only via the read-only
   truth endpoint, and the agent's guide says so.
2. **Tooling that lies.** `/status` serves a pre-incident snapshot (healthy, 12
   subscriptions; reality 96 and nothing serving). `/keys`, the obvious
   diagnostic, hangs 60 s *and adds compaction debt*. The aggregated metrics
   collector resolves scrape targets through the service it monitors, so it goes
   blind exactly when needed.
3. **Irreversible accumulating cost.** Dropped requests accrue into a counter no
   action decreases (budget 20× floor; reference recovery loses zero).
   `force-reset` destroys a member permanently — full service on a cluster with
   no redundancy left is graded `cluster_redundancy_destroyed`, and losing a
   majority is `quorum_permanently_lost`.

Measured on the state machine:

| Strategy | Elapsed | Admitted | Regressions | Dropped | Final capacity |
|---|---:|---:|---:|---:|---:|
| Flawless | 366 s | 1.00 | 0 | 0 | **1.00** |
| Rushed | 187 s | 0.00 | 1 | 300 | 0.10 |
| Never sheds | 300 s | 0.00 | 0 | 0 | **0.00** |

It declares `recommended_agent_timeout_seconds = 2700`. **Running it at 900 s
would measure the budget, not the agent** — that is the whole point.

---

## Your task, in order

### 1. Confirm the one discriminating result at n=10

`gitlab_regional_failover_single` failed 2 of 3. At n=3 that is 1 solve from
noise, and it is the only evidence in this whole effort that an environment here
separates a frontier agent — so it is worth a real measurement before anything is
built on it.

```sh
python scripts/evaluate_deathstarbench.py \
  --applications gitlab_ce --incident regional_failover --tiers single \
  --agent codex --model gpt-6-astra --agent-version 0.160.0 \
  --attempts 10 --profile svelte --agent-timeout 900 \
  --output results/failover-n10
```

Also run it on a second agent (`--agent claudecode --model claude-opus-5`) as its
own cohort. If both land near 1/3 the problem is real; if Codex is an outlier it
is a model quirk, not a benchmark property.

### 2. Build the next families around contested judgement

Not around duration. The roadmap candidates worth reading again with that lens
are the ones with a genuinely two-sided decision:

- **#4 GitHub 2018** — already built, and the one that works.
- **#12 Atlassian 2022** — hundreds of dependency-aware restores where
  per-customer validation can be got wrong.
- **#3 GitLab 2017** — already built, but currently a restore-and-replay
  procedure; it would need a contested choice added to discriminate.
- **#10 Knight Capital** — halt-versus-continue under accumulating loss is a
  judgement, and the loss meter already exists in the coordination runtime.

Avoid: longer recoveries, more impaired tooling, bigger state. All three were
tried and none of them bit.

### 3. Decide what to do with the two saturated families

`cascade` and `coordination` are both solved 3 of 3 under clean descriptions.
They are sound environments with working graders and full live admission, so they
are useful as *regression* tasks and as tier comparisons, but they do not
separate models. Either retire them from the difficulty set or add a contested
decision to each. Do not re-screen them hoping for a different answer.

### 4. What scaling up should mean on CloudLab

This machine was the constraint: **8 CPUs, 39 GiB RAM, 78 GB disk**. That forced
`single` tiers, one family at a time, and serial screens. With real hardware, in
priority order:

1. **n=10 cohorts**, so a 2-of-3 result can be distinguished from a 7-of-10 one.
2. **Both agents on every family**, as separate cohorts, to tell benchmark
   properties from model quirks.
3. **Parallel families** across separate DinD containers — the architecture
   already supports it, it was only ever resources.
4. **The `replicated` coordination tier** (510 s floor), never run.

## Operational notes that will cost you hours

These are all things that bit me.

### The inner Docker filesystem fills, and the symptom is misleading

A GitLab deploy that times out at `rollout status --timeout=1500s` with no useful
error is almost always **the inner Docker filesystem being full**, not a code
problem. Check first:

```sh
docker exec <dind> sh -lc 'df -h /var/lib/docker'
```

Measured, on what reclaims space:

- `docker volume prune` inside DinD: **useless** (dangling volumes are ~9 kB; the
  25 GB is in the four *active* KIND node volumes).
- `crictl rm` of exited containers: **~2 MB.**
- `crictl rmi --prune` on each KIND node: **~10 GB.** This is the fix.

```sh
for n in kind-control-plane kind-worker kind-worker2 kind-worker3; do
  docker exec "$n" crictl rmi --prune
done
```

Safe — only removes images no running container uses, all re-pullable (GitLab CE
re-pulled in 3m18s). **Each screen leaves several GB behind**, so a multi-family
campaign needs a prune *between families* or the later ones fail for reasons that
look like environment instability. On CloudLab, size the disk so this is moot.

A prune between families is **not sufficient on its own**. In one campaign the
between-families prune ran and the GitLab deploy still climbed to 91% with 2.6 GB
free while unpacking, against the 5.8 GB a successful run had had. Pruning the
three nodes *not* running GitLab mid-pull recovered 4.9 GB and it completed; the
unpack then returned the staging space on its own, settling at 66%. So: prune
immediately **before** a GitLab-family deploy as well, and watch the figure
during the unpack rather than only between families. Pruning a node that is not
the one pulling is safe while a pull is in flight.

### Pruning images breaks the one app that is built locally

`crictl rmi --prune` on the KIND nodes is the only effective way to reclaim the
inner filesystem (see above), but it deletes `sregym-stripe-marathon:<digest>`
along with everything else, and that image has no registry behind it: it is built
from `docker/stripe-marathon` by `scripts/prepare_saas_prototypes.py` and pushed
onto the nodes with `kind load docker-image`. With `imagePullPolicy: IfNotPresent`
and nothing to pull from, the pods sit in `ImagePullBackOff` and the only symptom
is the deploy stage timing out:

```
Deploy application: CalledProcessError: Command '['kubectl', '-n',
'stripe-marathon', 'rollout', 'status', 'deployment/stripe-worker',
'--timeout=900s']' returned non-zero exit status 1.
```

That cost one screen and 18 minutes of cluster time before I looked at the nodes.
The image is still in the DinD daemon — only the node copies are gone — so the
repair is a reload, not a rebuild:

```bash
docker exec sregym-difficulty-baseline \
  kind load docker-image sregym-stripe-marathon:<digest>
# the digest is content-addressed:
#   python -c "from sregym.service.apps.stripe_marathon import STRIPE_IMAGE; print(STRIPE_IMAGE)"
```

Reload it after any prune and before any `stripe_marathon` run. Every other
family pulls from a public registry and recovers on its own.

### The full test suite deploys to the live cluster

`tests/problems/test_stale_hostaliases_dns_poisoning_astronomy_shop.py::test_lifecycle_against_a_live_cluster`
is one of the pre-existing failures and it **deploys 28 astronomy-shop pods and
abandons them on failure**. They then compete with any running validation. Use
`--ignore=tests/problems` while anything is running, and delete a leftover
`astronomy-shop` namespace before starting.

### No host venv on this machine

`uv sync` fails with ENOSFC. Run tests through the DinD image's venv:

```sh
docker run --rm -v $PWD:/src -w /src \
  --entrypoint /opt/sregym/.venv/bin/python sregym-dind:postmortems \
  -m pytest tests/oracles -q -p no:cacheprovider
```

Mount read-write (`init_logger` creates `./logs` at import). It runs as root and
leaves root-owned `__pycache__`; clean from inside a container. Cluster-dependent
tests (`tests/service/apps/`, `tests/integration/`) need the long-running DinD
container — copy the tree to `/tmp/<name>` there (4 GB tmpfs) rather than editing
`/opt/sregym`, whose source is a deliberately frozen calibration snapshot.

About 16 test failures are pre-existing and unrelated (`tests/docker/`,
`test_kind_scripts.py`, `kubectl_tool_tests/`, `test_mcp_port_reclaim.py`, and
`tests/dind/...rejects_nonroot...` which cannot pass inside DinD). **Diff failure
sets against a baseline run rather than reading a raw count.**

`ruff` is not installed but exists at
`/home/hackson/.cache/uv/archive-v0/u5zXQBEMmDRdxOd7/bin/ruff`.

### Claude Code auth needs the credentials file, mounted *and* copied

Two defects I fixed; the second is non-obvious. The client only accepted env-var
auth, and it overrides `CLAUDE_CONFIG_DIR` to its sessions dir — so the CLI stops
reading the mounted `~/.claude` even if the check passes. Proven:

```
CLAUDE_CONFIG_DIR overridden, file mounted but not copied → "Not logged in"
same, credentials copied into that dir                    → "OK"
```

Both fixed (`e124e488`). On a fresh machine, launch the DinD container with
`--claude-auth-file ~/.claude/.credentials.json` (the option exists for this);
for an already-running container, `docker cp` it to
`/root/.claude/.credentials.json`, mode 600. Check token expiry before a long
campaign — a 4-hour run on an 8-hour token is fine, but plan it.

### A file bind mount pins an inode, so re-logging in does not reach a container

`docker/dind/run.py` bind-mounts `~/.codex/auth.json` (and
`~/.claude/.credentials.json`) as *files*. A fresh `codex login` **replaces** the
file rather than editing it, so the host path gets a new inode and the running
container keeps serving the old credentials indefinitely. Symptom: the model
reports unsupported even though login just succeeded on the host.

Diagnose by comparing inodes, not mtimes:

```sh
stat -c %i ~/.codex/auth.json
docker exec <dind> stat -c %i /root/.codex/auth.json
```

You cannot `docker cp` over the mount point (`device or resource busy`). Either
restart the container — expensive, it takes the cluster with it — or stage the
fresh file at another path and point `CODEX_HOME` at it, which
`container_runner._mount_codex_credentials` honours when choosing what to copy
into agent containers:

```sh
docker cp ~/.codex/auth.json <dind>:/root/.codex-fresh/auth.json
# then run the campaign with CODEX_HOME=/root/.codex-fresh
```

On a fresh machine, prefer mounting the *directory* rather than the file so a
re-login propagates.

### `--validate-only` is not a dry run

It skips *agent attempts* but still deploys and validates. I used it to check
argument parsing and it began deploying GitLab onto a cluster already running a
screen. Nothing touches the cluster while a screen is running.

---

## Things I would not trust without rechecking

- **The `expanded` GitLab cohort sits at 2 valid passes**, closed because
  `gpt-6-astra` is not entitled for this host's ChatGPT account. It is not a
  difficulty result. If you want that cohort, rerun all three under one agent.
- **Every "0%" above is n=3.** Exploratory screens, not reliable estimates.
- **The coordination family has never faced an agent.** Its mechanics are
  unit-tested (42 tests) and its physics measured, but no agent has tried to
  break it. Expect to find at least one thing an agent does that the grader
  mishandles — that happened with *every* family here. Live admission on the
  cascade family caught a grading bug that would have failed an agent for
  scaling up correctly and being graded three seconds later, before its third pod
  was ready.
- **Scope claims in each family's doc are deliberate and load-bearing.** The
  divergence family uses one CNPG cluster with promotion modelled by snapshot
  restore, not real split-brain. The cascade applies latency via a control file,
  not packet loss. The coordination family is not Consul and has no BoltDB, and
  its clock is compressed. Keep those statements accurate as you extend things.

---

## Fast orientation

```
sregym/service/apps/incident_runtime/      executable incident mechanics
  coordination_store.py                    the long-horizon state machine — start here
  chat_gateway.py, capacity_scaler.py      the misleading-CPU cascade
sregym/service/apps/coordination_cluster.py  cluster + circular telemetry
sregym/conductor/problems/                 problem definitions
sregym/conductor/oracles/                  graders; SaaSOracle is the shared base
tests/integration/validate_*.py            per-family live admission
scripts/evaluate_deathstarbench.py         the campaign runner (--agent, --incident)
docs/deathstarbench/                       one page per family + calibration report
```

Conventions worth matching: oracle `FAILURE_CLASSES` hold **only their own**
reasons as literal string keys (`Oracle._failure_classes()` already merges the
MRO; a `**` spread breaks the static check in `test_failure_sweep.py`). Every
grader distinguishes agent error from environment error, because only the former
counts toward difficulty. Each family's doc states what it does *and does not*
reproduce — keep that honest, it is the most useful thing in them.

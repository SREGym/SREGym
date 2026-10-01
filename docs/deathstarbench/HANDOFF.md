# Handoff: environment scaling, and the task on CloudLab

Branch `feat/environment-scaling-postmortems`, 23 commits, 208 files, clean tree,
**nothing pushed**. Everything below is reproducible from that branch.

This document is for the agent picking the work up on CloudLab. Read
[the calibration report](difficulty-calibration.md) next; it is the running
record of every graded screen.

---

## The one result that matters

**Every family screened so far scores 0% difficulty.** Not "nearly saturated" —
zero failures in every valid graded attempt, with agents using a quarter to a
third of their time budget.

| Family | Agent | Valid attempts | Passes | Difficulty | Median agent time |
|---|---|---:|---:|---:|---:|
| GitLab database deletion, replicated | Codex `gpt-6-astra` | 3 | 3 | 0% | 420.7 s |
| GitLab notification recovery | Codex | 3 | 3 | 0% | 469.4 s |
| Stripe recurring config | Codex | 3 | 3 | 0% | 110.8 s |
| GitLab ambiguous notification | Codex | 3 | 3 | 0% | 465.0 s |
| GitLab intermittent SMTP | Codex | 3 | 3 | 0% | 664.3 s |
| GitLab delayed audit | Codex | 3 | 3 | 0% | 510.7 s |
| GitLab delayed audit, expanded (4×) | Codex | 2 valid, 1 invalid | 2 | — | 662.2 s |
| **Mattermost capacity cascade** | Claude Code `claude-opus-5` | 3 | 3 | **0%** | 237.7 s |
| **GitLab regional failover** | Claude Code `claude-opus-5` | 3 | 3 | **0%** | 263.5 s |

28 valid graded attempts. 28 passes.

**Read the failure mode correctly.** These families varied *volume* — topology,
state size, telemetry, tenancy, acknowledged-write accounting, grading contract.
The expanded tier quadrupled tenancy, backlog and audit records and changed
nothing. What they all share is a **short, forgiving recovery shape**: diagnose,
act once or twice, submit, comfortably inside 900 s. The agent times say the
budget was never the constraint.

I also verified the passes were substantive, not lucky — every cascade attempt
held capacity across the automation's decision window with zero requests shed,
and every failover attempt retained *both* acknowledged write sets with zero data
loss. The agents genuinely solved them.

**Corollary for scaling up: do not scale volume.** More tenants, more replicas,
more records will not move these numbers. The lever is recovery *shape*.

---

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
| **Coordination collapse** | **long horizon + broken tools + accruing cost** | **unscreened** |

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

### 1. Screen the coordination family at its real budget

**Live admission passed** on `coordination_collapse_single` in 883 s, clean
cleanup, every lever proved on a cluster rather than asserted — stale `/status`
reporting 12 subscriptions against a real 96, a rollout restart changing nothing,
three compaction probes each restarting the window they waited on, a rushed
admission costing 300 permanently dropped requests, a 393 s reference recovery
against a 360 s floor, and a `force-reset` failing the recovery one second after
it passed. Details in [coordination-collapse.md](coordination-collapse.md).

The `replicated` tier (510 s floor) has **never been run**. Admit it before
screening it:

```sh
PYTHONPATH=/opt/sregym python tests/integration/validate_coordination_collapse.py \
  --tier replicated --output results/coordination-admission-replicated.json
```

### 2. Screen it at its real budget

```sh
python scripts/evaluate_deathstarbench.py \
  --applications mattermost --incident coordination_collapse \
  --tiers single --agent claudecode --model claude-opus-5 \
  --attempts 3 --profile svelte --agent-timeout 2700 \
  --output results/coordination-screen
```

This is the first screen that can actually answer the question. Two outcomes,
both informative:

- **Non-zero difficulty** → recovery *shape* is the lever, and the roadmap should
  pivot to long-horizon families. Then build the next two or three on the same
  principle (candidates #2 Rogers, #11 AWS Kinesis, #14 CrowdStrike are the
  long-horizon ones) and screen at matched budgets.
- **0% again** → shape is not sufficient either, and the honest conclusion is
  that a 45-minute single-agent episode cannot be made hard by environment design
  alone. That would be a significant finding and should be written up as one
  rather than buried. The next lever would be *multi-incident* or *multi-day*
  episodes, which the current harness does not support.

**Do not pool cohorts.** Codex and Claude Code rows are separate. Changing agent,
model or budget starts a new cohort.

### 3. What scaling up should mean on CloudLab

This machine was the binding constraint: **8 CPUs, 39 GiB RAM, 78 GB disk at 88%
full**. That forced `single` tiers, one family at a time, and serial screens.
With real hardware, the valuable things are, in order:

1. **More attempts per cohort.** n=3 moves in 33% steps. n=10 would let you
   distinguish 0% from 10%, which matters enormously once something finally
   fails.
2. **Both tiers, and the `replicated` coordination tier** (510 s floor) which has
   never been run at all.
3. **Parallel families** across separate DinD containers — the architecture
   already supports it (`docker/dind/run.py`), it was only ever a resources
   problem. One container per family, matched settings.
4. **Longer budgets as an explicit variable.** Screen coordination collapse at
   2700 s and at 5400 s. If difficulty drops as budget rises, the environment is
   gated on time rather than on reasoning — worth knowing either way.

---

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

---
name: sregym-db-bug-reproduction
description: >-
  Guide to SREGym's automatic problem-construction pipeline, which turns a database bug report
  (GitHub or Jira issue) into a runnable SREGym problem. The pipeline resolves the buggy version,
  clones and builds a custom database image, deploys it on a kind cluster, swaps in the buggy image
  to inject the fault, runs a reproducer, and evaluates the agent's fix with oracles. Use this skill
  when the user wants to auto-generate or hand-craft an SREGym problem from a database issue,
  reproduce a database bug (Cassandra, TiDB, MongoDB, CockroachDB, or etcd), add a new database to
  DB_REGISTRY, or debug any stage of the issue-parsing, reproducer-extraction, image-build, deploy,
  fault-injection, or oracle-evaluation flow.
user-invocable: true
---

# Reproducing database bugs as SREGym problems

SREGym can take a public database bug report and automatically construct a benchmark "problem":
it deploys a real database cluster running the *buggy* build, triggers the bug, and grades whether
an agent diagnoses the root cause and mitigates it. This skill explains how that pipeline works and
how to operate, extend, and debug it.

## Mental model: two phases

1. **Construction** (issue URL → `auto_<db>_<number>.py`): parse the issue, find the buggy version
   and git ref, extract a runnable reproducer, optionally validate it, then render a problem file.
   Driven by `ProblemGenerator`.
2. **Runtime** (problem → graded run): clone source at the buggy ref, build a custom Docker image,
   deploy the *stock* cluster, swap in the buggy image to inject the fault, run the reproducer, and
   evaluate with oracles. Driven by `GenericCustomBuildProblem` + `GenericDBApplication`, orchestrated
   by the `Conductor`.

A generated problem is just a small subclass of `GenericCustomBuildProblem`. The heavy lifting is
shared infrastructure keyed off `db_name` via `DB_REGISTRY`.

## Quick start

### Auto-generate a problem from an issue URL

```python
from sregym.conductor.problems.problem_generator import ProblemGenerator

# Writes sregym/conductor/problems/auto_tidb_67650.py and returns "auto_tidb_67650".
problem_id = ProblemGenerator.generate("https://github.com/pingcap/tidb/issues/67650")
```

`generate()` is idempotent on the output path — re-running the same URL overwrites the file
(`problem_generator.py`, `ProblemGenerator.generate`). The new file is auto-discovered by
`ProblemRegistry` on next instantiation, so it can be run immediately by the Conductor.

Helpful environment variables before generating:

| Variable | Effect |
| --- | --- |
| `GITHUB_TOKEN` | Authenticated GitHub API calls (5000 vs 60 req/hr). Read in `GitHubIssueParser.__init__`. |
| `ANTHROPIC_API_KEY` | Enables LLM reproducer extraction/repair; without it, extraction falls back to regex only. |
| `JIRA_TOKEN` / `JIRA_EMAIL` / `JIRA_API_TOKEN` | Jira auth (Bearer or Basic) in `JiraIssueParser`. |
| `SREGYM_SKIP_REPRODUCER_VALIDATION=1` | Downgrades validation failures from hard errors to warnings (`validation_required()` in `reproducer_validator.py`). |

## Phase 1 — Construction (issue → `auto_*.py`)

Entry point: `ProblemGenerator.generate()` in `sregym/conductor/problems/problem_generator.py`.

1. **Route the URL.** `sregym/service/issue_parser.py:parse_issue()` dispatches by substring:
   `"github.com"` → `GitHubIssueParser`, else `"/browse/"` → `JiraIssueParser`, else `ValueError`.
2. **Resolve DB + version + ref.** `GitHubIssueParser.resolve()` (`sregym/service/github_issue_parser.py`)
   matches the repo to a `DBBuildSpec` (via `github_repo`), then finds the buggy ref through a fallback
   chain: commit SHA in body → SHA in timeline → version label → milestone → semver in title/body.
   Returns a `ParsedIssue`.
3. **Extract the reproducer.** `sregym/service/reproducer_extractor.py:extract_reproducer_full(body)`
   returns a **7-tuple** in this exact order:
   `(reproducer, expected_output, buggy_output, correct_output, setup_preconditions, crash_on_startup, fault_injection_type)`.
   It tries the LLM extractor first, then a regex fallback only if no reproducer and not a startup crash.
   It returns nothing (or discards the candidate) for Sentry-auto-filed issues, stack-trace/panic-only
   bodies, prose-only blocks, redacted SQL, and cases where `buggy_output` appears verbatim inside the
   reproducer (a circular match that usually means a traceback was captured by mistake).
4. **Validate (best-effort).** `sregym/service/reproducer_validator.py:validate_reproducer()` runs the
   reproducer in a throwaway Docker container of the *stock* image and compares output to
   `buggy_output`/`correct_output`. **Real validators exist only for `mongodb` and `cockroachdb`**; every
   other DB (and any case where Docker/image is unavailable) is `skipped` (inconclusive). On failure the
   generator asks the LLM to repair the reproducer and re-validates, up to `_MAX_REPAIR_ATTEMPTS = 2`.
   A hard failure aborts generation unless `SREGYM_SKIP_REPRODUCER_VALIDATION=1`.
5. **Render the file.** `ProblemGenerator._render()` emits a `GenericCustomBuildProblem` subclass.
   - Normal reproducer → `reproducer`, `continuous_reproducer`, optional `expected_output` and `_setup_preconditions_sql`.
   - `crash_on_startup` → `crash_on_startup = True` + a stub `setup_preconditions()` to fill in.
   - `fault_injection_type == "node_kill"` → an `inject_fault()` override that runs the reproducer in the
     background, kills a random DB pod, then joins.
   - `buggy_output`/`correct_output` are **validation-only** and never written to the file.
   Naming: `problem_id = auto_{db}_{number}`, class `Auto{Db}{number}` (GitHub `/issues/N`, Jira `/browse/PROJECT-N`).

## Phase 2 — Runtime (build → deploy → inject → evaluate)

Base class: `GenericCustomBuildProblem` in `sregym/conductor/problems/generic_custom_build.py`.

1. **`__init__`** looks up `DB_REGISTRY[db_name]`, applies per-problem `build_cmd`/`build_image`
   overrides, resolves `(version, git_ref)`, clones source via
   `sregym/service/source_manager.py:SourceManager.ensure_source()` (cached under `/tmp/sregym-sources`,
   shallow clone with a fetch+checkout fallback for commit SHAs), then builds the custom image with
   `sregym/service/generic_db_build_manager.py:GenericDBBuildManager`.
2. **Build image.** `build_from_directory()` (or `build_with_patches()` for hand-crafted patches) hashes
   the source/patch tree for cache-keyed reuse. For Go projects, `_resolve_build_image()` reads `go.mod`
   and overrides the toolchain image. `prebuilt_from_stock=True` skips compilation and just re-tags the
   stock base image (used when the bug already ships in the public image). The image is loaded into the
   cluster via a `kind load` → SSH → privileged DaemonSet fallback chain.
3. **Deploy stock cluster.** The Conductor calls `problem.app.deploy()` during setup
   (`conductor.py:deploy_app`). `GenericDBApplication.deploy()` installs the operator/Helm chart and waits
   for the cluster to be Ready. Two deploy modes (see `helm_deploy_chart`): operator + CR, or the Helm
   release *is* the cluster (no CR).
4. **Inject the fault.** The Conductor calls `inject_fault()` exactly once before the first agent stage
   (`conductor.py:_advance_to_next_stage`, when `start_index == 0 and not fault_injected`).
   `inject_fault()` swaps the running cluster to the buggy image (`inject_buggy_image()`, with an
   operator-override fallback that scales the operator to 0 and patches StatefulSets directly), runs
   `setup_preconditions()`, runs the reproducer, and — if `continuous_reproducer` — deploys a looping
   reproducer Deployment. `crash_on_startup` problems instead use `inject_buggy_image_expect_crash()` and
   wait for CrashLoopBackOff.
5. **Evaluate / recover.** Mitigation is graded by an oracle (see below). `recover_fault()` restores the
   stock image.

## Problem class anatomy (`GenericCustomBuildProblem`)

**Required:** `db_name` (key into `DB_REGISTRY`), plus **either** `issue_url` (auto mode) **or** both
`db_version` and `source_git_ref` (hand-crafted mode).

**Optional overrides** (all defined near the top of `generic_custom_build.py`):
`root_cause_description`, `root_cause_file`, `reproducer`, `expected_output`, `continuous_reproducer`,
`crash_on_startup`, `_setup_preconditions_sql`, `extra_helm_args`, `build_cmd`, `build_image`,
`patch_dir` (hand-crafted patch overlay).

### Hand-crafting a problem (when auto-generation can't reproduce it)

Set `db_version` + `source_git_ref` instead of `issue_url`, write the `reproducer` explicitly, and pick
the oracle behavior with `continuous_reproducer` / `expected_output` / `crash_on_startup`. See
`sregym/conductor/problems/auto_etcd_18089.py` for a clean hand-crafted example (deterministic logic bug
with a continuous reproducer), and `auto_zookeeper_2213.py` for a stub. For source-patch bugs, point
`patch_dir` at a directory of files to overlay before building.

## Reproducing a Cassandra bug by hand (operational playbook)

Validated on 85 Cassandra reproductions. The point of reproducing by hand first is to produce the evidence
log that the Problem is then built from.

**Stock-image fast path (use this for any deployable bug).** A bug fixed in a released `X.Y.Z` patch is
present in the `cassandra:<X.Y.(Z-1)>` image, so the buggy version = **fix patch − 1** and the stock image
already contains the bug — no source build. If the fix patch also has a released image, run the identical
workload on it as an **A/B control**. Released-image ceilings on Docker Hub `library/cassandra`:
**3.11→19, 4.0→20, 4.1→11, 5.0→8** (no `5.0.9 / 4.0.21 / 4.1.12 / 6.x / 7.x`). Bugs fixed only in trunk
(`6.0-alpha*/6.0/6.x/7.x`) have no image → `blocked-no-image` (would need a source build).

**Single-node** (CQL-semantics, most storage bugs): one stock pod, `MAX_HEAP_SIZE=1024M`,
`CASSANDRA_ENDPOINT_SNITCH=GossipingPropertyFileSnitch`; drive via `kubectl exec ... cqlsh`/`nodetool`.
Config-gated bugs: append a `cassandra.yaml` block in the pod command before `docker-entrypoint.sh
cassandra -f`. Compaction-iteration bugs: `nodetool disableautocompaction` + `INSERT`+`flush` ≥2× to
retain ≥2 sstables, then the triggering `nodetool` command.

**Multi-node ring** (distributed bugs): headless `Service` + `StatefulSet`
(`podManagementPolicy: OrderedReady`, seed `cass-0`, `CASSANDRA_SEEDS=cass-0.cass.<ns>.svc...`); wait for
`nodetool status` to show N × `UN`. To create **per-replica divergence** on the same partition key without
an in-JVM dtest, use **gossip isolation**: `nodetool disablegossip` on the other nodes, confirm they show
`DN` from the writer's view, write at `CONSISTENCY ONE` with explicit `USING TIMESTAMP`, `nodetool flush`,
then re-enable gossip. Verify the physical divergence with `sstabledump` on each node's local `Data.db` —
a `CL=ONE` read is routed to an arbitrary replica and is **not** a reliable per-node probe. (This
reproduced CASSANDRA-21332, which looked "in-JVM-dtest-only".)

**Evidence bar.** A reproduction needs a **verbatim** buggy signature (exact exception + frame, server
error, or wrong-result row) plus the A/B control where a fixed image exists. Write it all to
`.claude/repro-evidence/repro-CASSANDRA-<n>.md`.

**Dispositions** (use the precise one; all but `reproduced` are clean outcomes): `reproduced` /
`not-reproducible` (buggy path shadowed by earlier validation or a disabled feature) / `not-observable`
(internal refinement, no client/operator-visible change — a poor benchmark candidate) / `confirmed-blocked`
(name the un-stageable mechanism: in-JVM message interception or `executeInternal`, a precise
timing/partition window, a crash between two syscalls, or no concrete reproducer) / `blocked-disk-constrained`
(reproducer needs more data than the disk budget) / `needs-fix-test` (reproducer lives only in the fix's
test) / `blocked-no-image` (trunk-only).

**Triage before reproducing.** Filtering candidates first pays off: naive "DB-behavior" filtering reproduced
~22%, whereas gating on an **observable symptom** + a **concrete/derivable reproducer** + a
**stageable reproduction-shape** reproduced ~76%. Watch for cyber-safeguard false-positives on
security/CVE/auth bugs (assess those read-only by hand). See **Running this at scale** below for the
disk/concurrency discipline when reproducing or generating many at once.

## From a reproduced bug to a benchmark Problem

Every reproduced DB bug should become a runnable benchmark Problem. The **authoritative source** for each
bug is its reproduction evidence log at `.claude/repro-evidence/repro-CASSANDRA-<n>.md` (machine-readable
mirror: `.claude/repro-evidence/candidate_results.json`). Read it — not memory — for the buggy version, the
EXACT reproducer steps, the verbatim buggy signature, and the A/B control. Trust the log over recollection.

### Pick a base class

| Base class | File | Discovery | Oracles | When |
| --- | --- | --- | --- | --- |
| `GenericCustomBuildProblem` | `auto_<db>_<number>.py` (e.g. `auto_cassandra_20050.py`) | **Auto** (`ProblemRegistry._load_auto_generated()`, no registry edit) | diagnosis **and** mitigation (mitigation only when `continuous_reproducer=True`) | **Preferred** for stock-reproducible Cassandra bugs in `bugs.txt`. |
| `CassandraBugProblem` | `cassandra_<number>.py` (e.g. `cassandra_20108.py`, `cassandra_18105.py`) | **Manual** (import + `PROBLEM_REGISTRY` entry in `registry.py`) | diagnosis-only (`mitigation_oracle = None`) | Hand-written custom `inject_fault`, no mitigation oracle needed. |

`GenericCustomBuildProblem` always gets a diagnosis `LLMAsAJudgeOracle` on the root cause; it *additionally*
gets a `ReproducerPodMitigationOracle` on a looping reproducer pod **only when `continuous_reproducer=True`**.
For a bug that already ships in the released image (buggy version = fix patch − 1), set
`prebuilt_from_stock = True` so it deploys the STOCK buggy image instead of running a ~30-min source build
(`ant jar`). `CassandraBugProblem` deploys a stock cluster via the K8ssandra operator and runs `trigger_cql`.

### Pick the reproduction shape (read the evidence log first)

- **Single-node, pure CQL** (CREATE/INSERT/DELETE/SELECT that triggers the bug): `GenericCustomBuildProblem`
  with `reproducer` = the CQL block and `continuous_reproducer = True`. (`auto_cassandra_20050` style.)
- **Wrong-result bug** (returns/persists an incorrect value rather than erroring): ALSO set `expected_output`
  to the buggy value so the mitigation probe greps for it (Ready = bug present, NotReady = fixed).
- **Config-gated** (needs a `cassandra.yaml` block such as `startup_checks`/`guardrails`, or a pre-staged
  file): use `_setup_preconditions_sql` or override `setup_preconditions()`; for startup-failure bugs set
  `crash_on_startup = True` (inject runs preconditions, swaps the buggy image, waits for CrashLoopBackOff).
- **nodetool / flush sequence** (e.g. `disableautocompaction` + flush × N + `garbagecollect`): override
  `inject_fault()` to run the nodetool steps via `kubectl exec` (see `cassandra_20108.py` for the
  kubectl-exec + background-loop pattern), then run the CQL.
- **Multi-node ring or cross-version** (per-replica divergence, scale/bootstrap, repair, `sstableloader`
  between versions): these need multi-pod orchestration a single `reproducer` CQL string CANNOT express.
  Write a **clearly-marked STUB** — set `db_version`/`source_git_ref`/`root_cause_*` and put the full
  multi-node steps from the evidence log in a `reproducer`/docstring TODO — rather than flattening a
  multi-node reproduction into one CQL. A flattened version **compiles and registers but silently does NOT
  reproduce the bug**, which is worse than an honest stub.

### Required fields (GenericCustomBuildProblem pattern)

- `db_name = "cassandra"`
- `db_version = <buggy>` (= released fix patch − 1)
- `source_git_ref = "cassandra-<buggy>"`
- `root_cause_file` (the buggy source file) and `root_cause_description` (1–3 sentences)
- `reproducer` (the CQL/steps)
- `continuous_reproducer = True`
- `prebuilt_from_stock = True` for stock-image bugs (`bool | None`: `None` = inherit the `DBBuildSpec`
  default; `True` = skip the build and re-tag the stock image)
- `expected_output` **only** for wrong-result bugs

### Oracle semantics

Diagnosis = `LLMAsAJudgeOracle(expected=root_cause)`. Mitigation = `ReproducerPodMitigationOracle` with
`expect_unready = (expected_output is not None)` — see **Oracles & readiness semantics** below for the full
Ready/NotReady mapping (wrong-result: Ready = bug present; error/crash: NotReady = bug present).

### Registration

`auto_*.py` whose class subclasses `GenericCustomBuildProblem` are auto-discovered (the loader checks
`issubclass(GenericCustomBuildProblem)`); the problem id is the file stem (e.g. `auto_cassandra_20050`).
`cassandra_*.py` / `CassandraBugProblem` need a manual `import` + a `PROBLEM_REGISTRY` entry in `registry.py`.

### Verify STATICALLY only (CRITICAL)

NEVER instantiate or deploy a generated Problem to "verify" it — `__init__` triggers the image build /
operator deploy and is slow and disk-heavy. Instead:

1. `uv run python -m py_compile <file>` — it parses.
2. Confirm `ProblemRegistry()` registers the id (e.g. `<id> in ProblemRegistry().PROBLEM_REGISTRY`). The
   loader **stores the class and does NOT call `__init__`**, so this check is cheap and safe.
3. Spot-check that the encoded `reproducer` matches the **buggy** path in the evidence log (not the A/B
   control).

## Running this at scale

When reproducing or generating many problems at once (dozens+), the binding constraints are **disk** and
**workflow orchestration**. Hard-won discipline (see also the `workflow-fanout-at-scale` skill for the
general patterns):

- **Disk first.** Each Cassandra pod/ring is heavy; a small host disk (the run here had 63 GiB) fills fast.
  Run reproductions in **bounded-concurrency waves** (a worker pool of 3-8, not unbounded), `kubectl delete
  ns` after each agent, and `crictl rmi --prune` inside the kind nodes between waves — accumulated images
  in the kind nodes' containerds (not pod data) are the main hog and pruning reclaims many GiB at once.
  Tear down idle clusters you no longer need. Problem **generation** (writing `auto_*.py`) is code-gen and
  disk-light, so it can run at full parallelism.
- **Pilot-gate** any large fan-out: run ~10 first, verify teardown actually happened, the `reproduced`
  verdicts have their verbatim signature present in the log, and the rate is sane — then release the rest.
- **Pass batches via a file**, not workflow `args` (args delivery proved unreliable): write
  `/tmp/repro_batch.json` and have a trivial reader agent return it.
- **Worker pool over chunked parallel:** a chunk barrier lets one slow agent block its whole chunk; a pool
  lets a slow agent occupy only its own slot.
- **Idempotent/resumable:** skip any item with a per-item done-marker (here, `/tmp/repro-<key>.md`), so a
  re-run only does what's left.
- **Verify code-gen statically** (`py_compile` + `ProblemRegistry()` loads the class) — **never instantiate
  a Problem to verify it**, because instantiation triggers the image build / cluster deploy.

## Adding a new database

Add a `DBBuildSpec` entry to `DB_REGISTRY` in `sregym/service/db_build_spec.py` describing the four phases
(Source / Build / Package / Deploy) and supply a `run_reproducer_fn` (and optional
`reproducer_workload_fn` for continuous reproducers). See `reference.md` for the full field list and a
per-DB cheat-sheet. After editing, **always** confirm the module imports — a syntax error here silently
breaks auto-loading of *every* generated problem (see Known bugs).

## Oracles & readiness semantics

Continuous-reproducer problems use `sregym/conductor/oracles/reproducer_pod_mitigation.py:ReproducerPodMitigationOracle`,
which reads the reproducer pod's readiness. The mapping is controlled by `expect_unready`, which
`generic_custom_build.py` sets to `expected_output is not None`:

- **Wrong-result bugs** (`expected_output` set → `expect_unready=True`): the probe greps for the buggy
  value, so **Ready = bug present, NotReady = fixed**.
- **Crash/error bugs** (no `expected_output` → `expect_unready=False`): the probe checks the exit code, so
  **NotReady = bug present, Ready = fixed**.

## Known bugs & gotchas

- **Jira parser unpack mismatch.** `extract_reproducer_full()` returns 7 values, but
  `JiraIssueParser.resolve()` (`sregym/service/jira_issue_parser.py:56-58`) unpacks only 5 — this raises a
  `ValueError` at runtime and also drops `setup_preconditions`/`fault_injection_type`. GitHub issues work;
  Jira issues are currently broken until this is fixed.
- **Registry typo.** `sregym/conductor/problems/registry.py:279` registers `"auto_tidv_67002"` (should be
  `"auto_tidb_67002"`).
- **Auto-load is all-or-nothing on shared imports.** `ProblemRegistry._load_auto_generated()` catches
  per-file import errors, but a syntax/import error in a *shared* dependency like `db_build_spec.py` breaks
  loading for all generated problems. Compile-check after editing shared modules:
  `uv run python -m py_compile sregym/service/db_build_spec.py`.
- **`run_reproducer_background()` annotation is wrong.** It returns a `threading.Thread`, not a
  `subprocess.Popen` (`generic_db_app.py`). Generated `node_kill` injectors call `t.join()`, so runtime is
  fine, but the type hint/docs are misleading.
- **Reproducer validation is narrow.** Only `mongodb` and `cockroachdb` are actually validated before a
  file is written; for other DBs a "successful" generation does not mean the reproducer fires the bug.
- **Distroless DB images** (e.g. `quay.io/coreos/etcd`) have no shell — reproducers run from a separate
  client pod (etcd uses an `alpine:3.20` pod that downloads `etcdctl`).

## Key files

| File | Role |
| --- | --- |
| `sregym/conductor/problems/problem_generator.py` | Issue → `auto_*.py` generator + validation/repair loop. |
| `sregym/service/issue_parser.py` | Routes a URL to the GitHub or Jira parser. |
| `sregym/service/github_issue_parser.py` / `jira_issue_parser.py` | Resolve DB spec, version, git ref; build `ParsedIssue`. |
| `sregym/service/reproducer_extractor.py` | LLM+regex reproducer extraction with safety guards. |
| `sregym/service/reproducer_validator.py` | Docker-based reproducer validation (mongodb, cockroachdb). |
| `sregym/conductor/problems/generic_custom_build.py` | Base problem class; build/deploy/inject/recover lifecycle. |
| `sregym/service/generic_db_build_manager.py` | Builds/re-tags custom images and loads them into the cluster. |
| `sregym/service/apps/generic_db_app.py` | Deploys clusters, swaps images, runs reproducers, cleans up. |
| `sregym/service/source_manager.py` | Clones/caches source at a specific git ref. |
| `sregym/service/db_build_spec.py` | `DBBuildSpec` schema + `DB_REGISTRY` + per-DB reproducer helpers. |
| `sregym/conductor/oracles/reproducer_pod_mitigation.py` | Readiness-based mitigation oracle for continuous reproducers. |
| `sregym/conductor/problems/registry.py` | Static registry + `_load_auto_generated()`. |

## Reference

See `reference.md` (next to this file) for the full `DBBuildSpec` field list grouped by phase and a
per-database cheat-sheet (build mode, deploy mode, client image, and reproducer command).

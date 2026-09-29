# Gitea database deletion and recovery

This is the first stateful incident family on the scaled applications. A
maintenance reset intended for a disposable database deletes Gitea's `public`
schema on the writable primary. The replicated tier's physical standbys replay
the same deletion. Selecting another member cannot recover the lost history.
Git objects remain on their independent repository volume.
The [evaluation report](database-recovery-results.md) records live validation and
Codex results separately from the original selector-fault comparison.

The task borrows the primary/replica confusion, unreliable backup expectations,
and data-loss accounting concerns from the
[2017 GitLab database incident](https://about.gitlab.com/blog/postmortem-of-database-outage-of-january-31/).
It is a new executable Gitea scenario, not an exact historical reconstruction:
the injected operation deletes a schema rather than a PostgreSQL data directory,
and an independent application journal makes all acknowledged issues recoverable.

| Tier | PostgreSQL members | Additional historical issues | Post-backup issues to reconcile | Persistent volumes |
|---|---:|---:|---:|---:|
| `single` | 1 | 12 | 6 | 3 |
| `replicated` | 3 | 60 | 30 | 5 |

Both tiers keep one Gitea server, five seeded repositories, the same 10 requests/s
read workload, the same causal failure, and the same recovery invariants. State
volume and database membership vary together; this is an exploratory scaling
screen, not an experiment isolating either variable's effect.

## Incident and recovery surface

The `recovery-console` pod has an independent 1 GiB archive PVC and the pinned
PostgreSQL client's `psql`, `pg_dump`, and `pg_restore`. Its database environment
uses the application role and writable service. It contains:

- `/recovery/README.txt`: the operational requirements and archive/journal format.
- `/recovery/operations.log`: the maintenance session's target and observed symptoms.
- `/recovery/backups/daily.dump`: a real complete custom-format `pg_dump` archive.
- `/recovery/backups/latest.dump`: a real archive truncated to its header after an
  uploader reported completion. The catalog's original checksum exposes this.
- `/recovery/journal/acknowledged-issues.json`: post-snapshot accepted issue
  creations, in order, with repository, public number, title, and body.

The archive is taken after baseline checks and historical seeding. Subsequent
issues are created through Gitea's API and their receipts are saved on the
independent archive volume. All database members must contain this state before
injection. The recent complete archive is then truncated in place, leaving no
hidden complete recent copy. Expected business state stays in the conductor's
memory, outside the evaluated agent's filesystem.

The deletion is a one-time operation. The app and PostgreSQL processes continue
running, while database-dependent requests fail. Recovery can involve stopping
writers, choosing and restoring an archive, bringing the app back, and replaying
or otherwise reconciling recent accepted writes. The grader accepts any method
that restores the required outcomes. The reference recovery uses an atomic
`pg_restore` transaction followed by ordered API replay.
[PostgreSQL archive formats](https://www.postgresql.org/docs/16/app-pgdump.html),
[restore options](https://www.postgresql.org/docs/16/app-pgrestore.html).

## Grading and validation

The incident oracle extends the existing Gitea persistence and replication
oracle. It requires all pre-deletion account identities and selected access
flags, repository identities and privacy settings, and issue identities and
content on every database member. Issue public identity is repository plus issue
number; internal SQL row IDs and timestamps may change during replay. It rejects
missing or changed records and duplicate replayed issues, while allowing new work.

It also checks all pre-deletion Git tree entries, the original archive checksum,
the original PVC identities, the configured replica count and write durability,
all journal receipts through the API, and a fresh issue/file workflow. It reports
per-member missing/changed record counts and zero acknowledged data loss on
success. These projections do not cover every Gitea table or permission relation.

The negative-control validator tests more than the ordinary lifecycle:

1. Healthy application passes; deletion reaches every database member and fails.
2. The truncated backup is rejected before the reference restore changes production.
3. The old archive restores baseline API data, but the incident still fails because
   post-backup issues are missing.
4. A partial journal replay still fails.
5. Full replay passes; replaying again creates no duplicate records.
6. A Gitea restart retains the recovered state and original volumes.

The benchmark's ordinary lifecycle separately checks the problem class's
`recover_fault()` implementation, followed by clean namespace and volume reclamation.

## Run in DinD

Build the working tree and run the two tiers with the configured comparison model:

```bash
python3 docker/dind/run.py build
python3 docker/dind/run.py run --name gitea-recovery --memory 32g \
  --codex-auth-file "$HOME/.codex/auth.json" -- bash -lc '
    python scripts/install_cnpg.py &&
    python scripts/evaluate_deathstarbench.py --applications gitea \
      --incident database_deletion --model gpt-6-astra --attempts 3 \
      --agent-version 0.157.0 --profile svelte --output results/database-recovery
  '
```

Use `--validate-only` to omit model attempts. The registered problem IDs are
`gitea_database_deletion_single` and `gitea_database_deletion_replicated`.
In a separately prepared cluster, run the negative controls before evaluating agents:

```bash
python tests/integration/validate_gitea_recovery.py --tier single \
  --output results/recovery-negative-controls-single.json
python tests/integration/validate_gitea_recovery.py --tier replicated \
  --output results/recovery-negative-controls-replicated.json
```

Keep operator installation outside the benchmark's initial baseline capture.
Run these commands serially and retain raw traces, lifecycle verdicts, source
hashes, failure classifications, and agent/oracle timings separately.
`--agent-version` creates a private registry copy for the campaign; it leaves
the default `agents.yaml` unchanged. The base image alone does not pin the CLI,
which the harness installs separately at startup.

The journal is a bounded application fixture, not PostgreSQL WAL or a complete
production event stream. This version does not implement point-in-time recovery,
physical backup rebuilding, a continuously growing write backlog, historical
reconstruction mode, or separate regional failure domains. Tmpfs-backed DinD
storage cannot establish disk performance or host-reboot durability. See the
[Gitea topology and version boundaries](gitea.md).

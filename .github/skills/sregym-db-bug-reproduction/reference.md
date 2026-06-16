# Reference: `DBBuildSpec` and `DB_REGISTRY`

Companion reference for the `sregym-db-bug-reproduction` skill. Source of truth:
`sregym/service/db_build_spec.py`.

## `DBBuildSpec` fields (grouped by pipeline phase)

A `DBBuildSpec` describes how one database type is sourced, built, packaged, and deployed. Problem
classes only declare `db_name`; everything else is keyed off this spec.

### Phase 1 — Source
- `name` — short key used in `DB_REGISTRY` and in generated `problem_id`s (`auto_<name>_<number>`).
- `repo_url` — git repository to clone.
- `github_repo` — `"owner/repo"`; used to match a GitHub issue URL to this spec.
- `version_tag_pattern` — maps a bare version to a git tag, e.g. `"v{version}"` or `"cassandra-{version}"`.

### Phase 2 — Build
- `build_image` — toolchain Docker image (JDK, Go, etc.). For Go, `go.mod` may override it at build time.
- `build_cmd` — shell command run inside `build_image` with the source tree as the working dir.

### Phase 3 — Package
- `artifact_glob` — glob (relative to source root) matching the compiled artifact.
- `base_image` — stock image to extend; may contain `{version}`.
- `artifact_dest` — absolute path inside the base image where the artifact is copied.

### Phase 4 — Deploy
- `operator_helm_repo`, `operator_helm_repo_url`, `operator_chart`, `operator_namespace` — Helm details.
- `default_cluster_name` — cluster name unless overridden per problem.
- `cr_kind` — lowercase CR kind (e.g. `tidbcluster`); unused when `helm_deploy_chart=True`.
- `cluster_manifest_fn(cluster, ns, version, custom_image)` — renders the CR manifest; `None` for chart-only DBs.
- `image_patch_fn(cluster, ns, new_image) -> dict` — JSON merge-patch to swap the running image.

### Optional callables / flags
- `prereqs_fn()` — operator prerequisites (e.g. cert-manager).
- `jira_project` — Jira project key so `JiraIssueParser` can map a URL to this spec.
- `run_reproducer_fn(cluster, ns, reproducer)` — runs a reproducer once against the live cluster.
- `reproducer_workload_fn(cluster, ns, reproducer, expected_output) -> str` — manifest (ConfigMap +
  Deployment) that runs the reproducer in a loop for continuous observability.
- `operator_extra_helm_args` — extra `--set`/`--values` flags appended to the operator/chart install.
- `prebuilt_from_stock` — skip compilation; the "custom" image is the stock base re-tagged (bug already
  in the public image). `build_cmd`/`artifact_glob`/`artifact_dest` are ignored.
- `helm_deploy_chart` — the Helm release *is* the cluster (no separate operator + CR). Image swaps go
  directly to the StatefulSet; `cluster_manifest_fn`/`image_patch_fn`/`cr_kind` are unused.

### Helpers
- `git_ref(version)` → applies `version_tag_pattern`.
- `resolved_base_image(version)` / `resolved_artifact_dest(version)` → substitute `{version}`.

## `DB_REGISTRY` cheat-sheet

Currently registered databases (`db_build_spec.py`, `DB_REGISTRY`):

| `db_name` | Build mode | Deploy mode | Reproducer client | Reproducer command |
| --- | --- | --- | --- | --- |
| `cassandra` | Build from source (`ant jar`, JDK 11) | Operator + CR (K8ssandra; needs cert-manager) | pod `cassandra-cql-client`, image `cassandra:4.1` | `cqlsh` |
| `tidb` | Build from source (Go) | Operator + CR (tidb-operator) | pod `tidb-sql-client`, image `mysql:8.0` | `mysql -h … -P 4000` |
| `mongodb` | `prebuilt_from_stock` | Operator + CR (community-operator) | pod `mongodb-mongosh-client`, image `mongo:6` | `mongosh` |
| `cockroachdb` | `prebuilt_from_stock` | `helm_deploy_chart` (cockroachdb chart) | pod `cockroach-sql-client`, image `cockroachdb/cockroach:v24.1.4` | `cockroach sql --insecure` |
| `cockroachdb_errors` | `prebuilt_from_stock` | `helm_deploy_chart` | same as cockroachdb | `cockroach sql --insecure` |
| `etcd` | Build from source (`make build`, Go) | `helm_deploy_chart` (Bitnami etcd) | pod `etcd-repro-client`, image `alpine:3.20` (downloads `etcdctl`) | shell script with `ETCDCTL_ENDPOINTS` |

Notes:
- **`cockroachdb_errors`** clones the `cockroachdb/errors` library for diagnosis but deploys the normal
  CockroachDB image — bugs there surface via SQL on a running cluster.
- **`mongodb`/`cockroachdb`** use `prebuilt_from_stock` because their public source trees can't be built
  standalone; the source is still cloned for diagnosis oracles.
- **`etcd`** images are distroless, so reproducers run from a separate alpine client pod.

## Continuous reproducer probe semantics

`reproducer_workload_fn` builds a Deployment whose readiness probe encodes the bug state. Combined with
`ReproducerPodMitigationOracle` and `expect_unready` (= `expected_output is not None`):

- Wrong-result bug (`expected_output` set): probe greps for the buggy value → **Ready = bug present**,
  **NotReady = fixed**.
- Crash/error bug (no `expected_output`): probe checks exit code → **NotReady = bug present**,
  **Ready = fixed**.

## Validation coverage

`reproducer_validator.py:validate_reproducer()` only implements real validators for `mongodb` and
`cockroachdb`; all other databases are reported as `skipped` (inconclusive). Treat a generated problem
for an unvalidated DB as unverified until you run it end-to-end.

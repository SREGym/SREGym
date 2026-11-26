# Airport meal stream interface

Import `Stream` from `sregym.conductor.problems.airport_meal_stream`. The
controller uses the runner's kubectl context and requires no Python Kafka
dependency. `Stream(namespace="airport-meals", *, kubeconfig=None,
context=None, artifact_dir=None)` targets a single namespace. Pass a
kubeconfig path and the context name for isolated lab runs.

## Deployment and artifacts

- `deploy(*, offsets_retention_minutes=10080, offsets_retention_check_interval_ms=600000)`
  installs the Kafka broker, topics, shared stream storage and Flink 1.18.1
  session. The namespace must already exist. This waits for the broker and
  TaskManager to be ready. The fixed session is deployed on first use.
- Run `bash build.sh` from this directory on the build host. Supply the
  resulting `dist/` directory as `artifact_dir` or set
  `AIRPORT_MEAL_STREAM_ARTIFACTS`. The directory must contain
  `meal-orders-1.18.1.jar`, `meal-orders-1.19.0.jar` and `kafka-ops.jar`.
  Stage the directory alongside the conductor checkout on the runner.
  Generated files stay out of source control. Deploy uploads the job JARs
  to the shared PVC and installs the small Kafka operations JAR in a ConfigMap.
- `archive(label, data)` stores a JSON value on the shared PVC and returns
  its file URI. `read_artifact(label)` returns that value. Labels consist of
  letters, digits, dots, hyphens and underscores. `artifacts()` lists labels.
  Keep original and later savepoint URIs alongside the relevant release records.
- `undeploy_session(version)` removes that session's pods and service after
  all jobs have stopped. Both sessions can use the same persistent savepoints.
  The application's namespace lifecycle owns final deletion.

## Jobs

- `start(*, route="production", version="1.18.1", parallelism=5,
  group=None, start="earliest", offsets=None, offset_reset="none",
  compression=True, checkpoint_ms=2500, savepoint=None,
  restore_mode="NO_CLAIM", name=None)` submits a detached job, waits
  for all subtasks to run, and returns a job ID string. Routes are
  `production` (default group `meal-production`, topic `issue-orders`) and
  `rehearsal` (default group `meal-rehearsal`, topic `preview-orders`).
  `start` accepts `earliest`, `latest`, `committed` or `explicit`.
  For `explicit`, pass all 20 partitions as `offsets={partition: next_offset}`.
  For `committed`, supply a reset policy `none`, `earliest` or `latest`.
  Supply a savepoint URI to restore, optionally with `CLAIM` or `NO_CLAIM`.
  This path deploys the bundled session, waits for all subtasks and a
  completed checkpoint, and publishes the route's release ConfigMap.
- `restore_in_place(release, savepoint, *, parallelism)` submits a savepoint
  for the stopped job still named in the route's release ConfigMap. Pass the
  previous release and the URI from `stop()`. It submits the recorded JAR
  to the existing JobManager with the recorded job settings and a new
  reader name. It waits for the requested free slots, running subtasks and
  a completed checkpoint, publishes the replacement release, and returns
  the new job ID. Until that checkpoint, the route keeps the old stopped
  release and its URI. A retry queries the recorded replacement job ID and
  resumes it if it is still running. Choose the size separately. `available_slots(release)`
  returns the current JobManager's `slots-available` from `GET /overview`.
- `release(route="production")` returns the current release mapping or
  `None`. It contains `job_id, route, version, name, group, parallelism,
  start, offsets, offset_reset, compression, checkpoint_ms, savepoint,
  restore_mode, target_topic, reader, status`. A release stopped with a
  savepoint also has `stopped_savepoint`. Offset map keys are integers in the
  Python API and decimal strings in the stored release JSON.
  Set `jobmanager` to a Kubernetes resource such as
  `deployment/flink-live-jobmanager` if it differs from the bundled
  deployment for `version`. The resource can also be a `statefulset/<name>`
  or `pod/<name>`. It serves REST on port 8081 inside the container and
  mounts the shared archive and savepoint storage at `/stream-state`.
  Record either `jar_path`, the absolute path to the deployed JAR in that
  container, or `jar_id`, the retained upload ID from `GET /jars` on that
  JobManager. Older records default to the bundled deployment and
  `/stream-state/jars/meal-orders-<version>.jar`.
- `check_release_artifact(release)` reads the recorded JobManager. It checks
  the deployed JAR path or looks for the retained ID in `GET /jars`. It
  returns true for an available artifact and false for a confirmed missing
  or unusable artifact. Failed reads and malformed responses raise exceptions.
- `stop(job_id, *, drain=False)` stops with a canonical savepoint and returns
  the file URI after the job is terminal. The route records the operation ID
  before submission so a retry with the same drain setting can query it.
  The terminal release and URI are recorded before archiving. Archive
  failures are logged and the URI remains usable. Use `archive_job(job_id)`
  to retry a missing archive while the terminal job is still visible in Flink.
- `cancel(job_id)` cancels the job without creating a savepoint.
- `job(job_id)` and `checkpoints(job_id)` return the native Flink REST JSON
  for the job and its checkpoint history. `assignment_observation(job_id)`
  returns `complete`, `owners` and `reason`. Grade the owner counts only
  when `complete` is true. The mapping has all 20 integer partitions and
  lists `{"subtask": int, "attempt": int}` for each partition. A complete
  observation can report missing partitions and partitions with two owners.
  The TaskManager line must match the running attempt's host, thread and
  time window. Each subtask's distinct logged partitions must match its
  native Kafka assignment gauge. Missing evidence, ambiguous threads and
  changing attempts produce a reason and require a fresh read.
  `assignments(job_id)` retains the mapping API for history clients,
  including archived terminal jobs. An incomplete live read returns empty
  owner lists through this compatibility method.
- `archive_job(job_id)` keeps `release`, `job`, `checkpoints`,
  `source_vertex` (Flink REST details), `assignment_logs`, and
  `reader_conflicts` (overlapping job IDs, usually empty). Each log entry
  carries `subtask, attempt, host, timestamp, line`. The owner map is
  computed when requested. Older archives can still be read using their
  recorded attempt identities and raw lines.

## Broker operations

Actions are JSON objects with `action_id, benefit_id, carrier, cafe, revision,
amount_cents, kind, bank` and optional `predecessor`. The four kinds are
`grant`, `revise`, `withdraw` and `offer`. Produce in a batch of
`(partition, action)` pairs. The fixture controls partition choice.

- `produce(records, *, topic="allowances")` returns acknowledgments in
  input order as a list of `{"topic": str, "partition": int, "offset": int}`.
- `begin_transaction(records, *, topic="allowances", transactional_id=None)`
  returns a transaction with the same acknowledgment list at `.receipts`.
  Call `.abort()` or `.commit()` on it. As a context manager it aborts
  an unfinished transaction on exit. Use a separate `produce` call while
  it is open to model a second producer.
- `read_records(topic, *, isolation="read_committed", offsets=None)`
  snapshots the readable range and returns it in partition/offset order.
  `offsets` can provide inclusive starting offsets for any partitions.
  Rows have `topic, partition, offset, value`, where `value` is the
  Kafka UTF-8 string. `read_orders(topic="issue-orders", *, offsets=None)`
  returns the same coordinates plus parsed data. Order rows are
  `{"topic": str, "partition": int, "offset": int, "input_topic": str,
  "input_partition": int, "input_offset": int, "action": dict}`.
  Revised publishers also set `writer_job_id` to the 32-character job ID
  from the running Flink task. Older envelopes omit the field. Consumers
  add the output topic, partition and offset from Kafka. Join those
  coordinates to clearing's recorded order and use `writer_job_id` to
  compare output before and after a release change.
- `topic_id(topic="allowances")` returns the broker topic ID string.
  `bounds(topic="allowances", *, isolation="read_committed")` returns
  `{partition: {"start": int, "end": int}}`. The committed end is exclusive
  and reflects the last stable offset. `group_offsets(group, *, topic="allowances")`
  returns `{partition: next_offset}` with expired/absent partitions omitted.
  `set_group_offsets(group, offsets, *, topic="allowances")` sets explicit
  next offsets for an idle group.
- `topic_config(topic="allowances")` returns effective `{name: value}`
  strings. `alter_topic_config(settings, *, topic="allowances")` sets the
  supplied overrides. Use `None` to remove an override. The input uses
  delete retention. To create a new segment boundary, temporarily set
  `segment.ms` to `1` before writing a marker to every partition. To
  remove old segments through normal size retention, temporarily set
  `retention.bytes` to `1`, with the boundary already present. Restore
  the steady overrides after checking the broker floors and segment files.
- `segments(topic="allowances")` returns `{partition: [segment_base_offset]}`
  from the broker's current `.log` files.
  `set_offset_retention(minutes, *, check_interval_ms=600000)` updates the
  Kafka process settings and waits through the broker rollout. Prefer passing
  the short value to `deploy` when building the night history, then restoring
  the steady value before handoff. Offset expiry is broker-wide.

`segment_files(topic="allowances")` returns the broker filenames by integer
partition, including files pending physical deletion.
`source_metrics(job_id)` returns per-subtask mappings of Kafka assignment and
lag gauge names to Flink REST string values. Transaction `.commit()` and
`.abort()` return `{"status": "committed" | "aborted", "receipts": [...]}`.
See [OPERATIONS.md](OPERATIONS.md) for release commands and pinned images.

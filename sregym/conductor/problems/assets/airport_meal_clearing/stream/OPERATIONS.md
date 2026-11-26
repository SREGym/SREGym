# Meal order stream operations

Kafka runs at `kafka:9092` inside the namespace. The production job reads
`allowances` and writes `issue-orders`. The rehearsal uses its own group and
writes `preview-orders`. Each job uses committed input isolation and
at-least-once output. Consumers can identify a delivery by its Kafka topic,
partition and offset. The order value carries the original action and its
input coordinate.

## Build and stage

Use a Linux x86-64 build host with Bash, Python 3, curl and GNU coreutils.
Run `bash build.sh` and stage its `dist/` on the conductor runner. The script
checks the JDK, Flink distributions, Kafka distribution and shaded connectors
before compiling. It accepts a build cache with `MEAL_BUILD_CACHE` and a
distribution output directory with `MEAL_BUILD_DIST`. Point `MEAL_LAB_CACHE`
at the earlier spike's `.flink-lab/build` to reuse its checked downloads.
The conductor looks for `dist/` next to this document unless
`AIRPORT_MEAL_STREAM_ARTIFACTS` points elsewhere.

These images can be preloaded into a selected kind cluster. Load the digest
references used by the manifests so `IfNotPresent` can find them.

| Service | Image |
| --- | --- |
| Kafka 3.7.2 | `docker.io/apache/kafka@sha256:8bd63e1bd445e5e19427a4bdbcc3d23bf6efd774b058a41b36ba87fda7623e34` |
| Flink 1.18.1 | `docker.io/library/flink@sha256:f0e9ffd07f3d43291dfaf7e0609eb2871462193275d452affbe5fac091017ae3` |
| Flink 1.19.0 | `docker.io/library/flink@sha256:61355051b493c0b2784ae5755608798e7c54d9db0f8467e9e3f183e3a48e617a` |

For manual import, run the following on the remote kind host with the selected
node container, canonical tagged name and matching digest reference. Repeat
for each image in the table.

```sh
docker pull "$pin"
docker tag "$pin" "$tag"
docker save "$tag" | docker exec -i "$node" ctr -n k8s.io images import -
docker exec "$node" ctr -n k8s.io images tag --force "$tag" "$pin"
```

On the single-node cluster, the Kafka claim holds broker and group state.
The shared Flink claim holds JARs, checkpoints, savepoints and release
archives. Both Flink sessions can mount it during a cutover. The namespace
owns the claims. Stop workloads before removing the namespace.

## Inspect

```sh
ns=airport-meals
kubectl -n "$ns" get deployments,services,pvc
kubectl -n "$ns" get configmap stream-release-production -o jsonpath='{.data.release\.json}'
kubectl -n "$ns" exec deployment/flink-118-jobmanager -- ls /stream-state/archive
kubectl -n "$ns" logs deployment/flink-118-taskmanager
kubectl -n "$ns" exec kafka-0 -- /opt/kafka/bin/kafka-topics.sh \
  --bootstrap-server kafka:9092 --describe --topic allowances
kubectl -n "$ns" exec kafka-0 -- /opt/kafka/bin/kafka-consumer-groups.sh \
  --bootstrap-server kafka:9092 --describe --group meal-production
```

Use the group, job ID, JobManager and reader name in the current release
record. Fetch `GET /jobs/<job-id>` and then
`GET /jobs/<job-id>/vertices/<source-vertex-id>` from that JobManager. Read
the native assignment lines from the TaskManager pods hosting those attempts.
Compare the distinct partitions in each subtask's log with its
`assigned-partitions` gauge under the vertex's subtask metrics endpoint.
When the attempts stay stable and every count agrees, confirm all 20
`allowances` partitions each have one owner. Repeat an incomplete
observation after a failed or partial read. The archive keeps earlier
release records, REST details, checkpoint history and raw assignment lines.

## Publish a release or resize

For a resize, use the JobManager and JAR recorded in the current route
ConfigMap. Choose a different parallelism that fits the existing session
once the stopped job's slots are released. Inspect `slots-available` in
`GET /overview` after the stop. Check that the recorded path is readable in
the JobManager or that the retained ID appears in `GET /jars` before stopping.

The examples below run against the selected JobManager through
`kubectl -n "$ns" port-forward service/flink-118 18081:8081`. Use the
service for the recorded JobManager. Record a trigger ID before making the
stop request. Use that same ID to query the outcome if the reply is lost.
Record the savepoint URI and terminal job state when Flink confirms both.

```sh
rest=http://127.0.0.1:18081
request=$(tr -d '-' </proc/sys/kernel/random/uuid)
curl -fsS -H 'Content-Type: application/json' -X POST \
  "$rest/jobs/$job/stop" \
  -d "{\"drain\":false,\"formatType\":\"CANONICAL\",\"targetDirectory\":\"file:///stream-state/savepoints\",\"triggerId\":\"$request\"}"
curl -fsS "$rest/jobs/$job/savepoints/$request"
```

Use the recorded `jar_id` if the session retains the upload. For a
`jar_path`, upload the deployed file with curl inside the JobManager through
`POST /jars/upload` as the multipart field `jarfile`. Send the uploaded
filename's basename or the retained ID to `POST /jars/<jar-id>/run` with
entry class `airport.meal.MealOrders`.
Set `parallelism` and use these ten strings in `programArgsList`, in order.

1. Broker address `kafka:9092`
2. Consumer group for the route
3. Output topic for the route
4. Job name
5. Unique source name, such as `allowances-20261121T034500Z`
6. Snapshot compression flag `true` or `false`
7. Parallelism
8. Checkpoint interval in milliseconds
9. Starting position `earliest`, `latest`, `committed` or `explicit`
10. Uppercase committed reset policy, explicit comma-separated `partition:offset`
    entries, or `NONE` for earliest or latest

To restore, set `savepointPath` and `restoreMode` on the JSON body. Use
`NO_CLAIM` for the normal release procedure so older savepoints remain
available. For a new start, verify the input topic ID and offset vector as
appropriate before changing the active route. Wait for the new job's readers
and completed checkpoint. Save its ID and actual settings in the route's
`release.json` ConfigMap using the [release fields](INTERFACES.md#jobs).

If a cutover needs a new bundled session, substitute its name and pinned
image for `__SESSION__` and `__FLINK_IMAGE__` in `flink.yaml` and apply it
in the same namespace. The bundled session names are `flink-118` and
`flink-119`. Wait for both deployments and the registered slots. Remove
a retired session's deployments and service after its jobs stop.

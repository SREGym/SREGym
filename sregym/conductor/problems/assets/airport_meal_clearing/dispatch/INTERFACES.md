# Airport meal dispatch and issuance

Call `deploy(problem)` from `sregym.conductor.problems.airport_meal_dispatch`
after history finishes and the partner Services and Kafka topics exist. It
starts `deployment/meal-dispatch` and `deployment/meal-issuer` in
`problem.namespace`. The fixture verifies historical deliveries and seeds
the issuer group before the live bridge starts.

On the build host, run `bash build.sh` in this directory with a JDK 11 or
later and the `apache/kafka:3.7.2` image. Set `MEAL_DISPATCH_JDK` to the JDK
directory if it is off PATH. The build copies the image's libraries without
starting a container. Set `MEAL_DISPATCH_KAFKA_LIBS` to a directory containing
those libraries to build without Docker. Stage `dist/meal-dispatch.jar`
on the runner and set `AIRPORT_MEAL_DISPATCH_ARTIFACTS` to its parent
directory. The default is this directory's `dist/`. Deploy mounts the
small JAR in a ConfigMap and runs it with the pinned Kafka image.

## Desk dispatch

Desk intake stays open while dispatch is paused. Send `{"paused":true}` to
each carrier's `POST /dispatch-control` and read `GET /dispatch-control` to
confirm. Resume with `{"paused":false}`. Compare `GET /actions` and
`GET /dispatches` by action ID to see the queue and its delivered coordinates.
Dispatch checks the desk before each new send. A send already in flight can
finish and receive its acknowledgment after a pause.

Live actions use Kafka's positive Murmur2 hash of the UTF-8 key
`carrier + "\0" + benefit_id` modulo the allowances partition count. Dispatch
records the Kafka topic ID, partition and offset in the carrier's journal.
On restart, the worker resolves the prior producer transaction and checks
the retained committed log for sends with missing acknowledgments. A recovered
send keeps its original coordinate. Keep those records available until the
desk has recorded the acknowledgments.

## Issuance and handoff

The issuer consumes committed `issue-orders` as group `meal-issuer`. It
sends the same input and output topic IDs and coordinates used by history,
and copies the Flink `writer_job_id` when the publisher supplies it. Earlier
orders keep the fields originally recorded at their output coordinate.
A delivery advances the group's next offset after clearing and any required
café catalog and handout writes succeed. Restarting uses the group's committed
positions. Partitions without a usable position replay from their retained
start. Clearing uses the same order coordinates on each attempt. Café
catalog and carrier handout retries reuse the recorded voucher ID. When
retrying a sale at the café, use the original `request_id` from that sale.

The workload starts in legacy mode. In revision mode, clearing resolves the
action by carrier, benefit and revision. The issuer uses the durable
coordinate receipt for any handout and applies the benefit's current
authorization to its café entries. A revision can update an existing
supplied number. Preserve all numbers supplied during legacy issuance and
carry their accepted sales into the café's benefit-scoped spend check.

Use the normal Kubernetes scale commands to stop and start either worker.
Keep one replica of each. The issuer group and carrier journals persist
while the workers are stopped.

```sh
kubectl -n airport-meals scale deployment/meal-dispatch --replicas=0
kubectl -n airport-meals scale deployment/meal-issuer --replicas=0
kubectl -n airport-meals scale deployment/meal-issuer --replicas=1
kubectl -n airport-meals scale deployment/meal-dispatch --replicas=1
kubectl -n airport-meals rollout status deployment/meal-issuer
kubectl -n airport-meals rollout status deployment/meal-dispatch
```

To drain, pause both desks and scale dispatch to zero. Wait for its pod to
exit so any in-flight send can settle. Keep intake open. Let the production
Flink job finish processing through the resulting allowance boundary and
stop its publication using the stream release controls. Record the exclusive
`issue-orders` ends and wait for the issuer group's next offsets to reach
them on all partitions. Before changing to revision mode, send `{}` to
clearing's `POST /issuance-cutover` and read back `GET /issuance-cutover`.
Then set the mode and wait for its rollout. Start the required stream and
bridge workers and resume desk dispatch when ready.

```sh
kubectl -n airport-meals set env deployment/meal-issuer ISSUANCE_MODE=revision
kubectl -n airport-meals rollout status deployment/meal-issuer
kubectl -n airport-meals exec kafka-0 -- /opt/kafka/bin/kafka-consumer-groups.sh \
  --bootstrap-server kafka:9092 --describe --group meal-issuer
kubectl -n airport-meals logs deployment/meal-dispatch
kubectl -n airport-meals logs deployment/meal-issuer
```

Set `ISSUANCE_MODE=legacy` to change back. For a drain while transactions
are present, compare group commits against the read-committed ends from
`Stream.bounds("issue-orders")`. Use the clearing
`GET /orders/{output_topic_id}/{partition}/{offset}` and the individual
handout and catalog reads to inspect a delivery. Match the clearing order's
writer to the production release and its input position to the carrier's
keyed dispatch. The rehearsal has its own output topic.

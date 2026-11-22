# Airport meal clearing operations

The `airport-meals` namespace has two airline desks (`north` and `south`), four café tills (`gate-a` through `gate-d`), and `clearing`. Each is reachable through its Kubernetes Service on port 8080. `GET /health` reports the site, role, and SQLite version. `GET /state` reads one site's business records in a consistent snapshot.

```sh
kubectl -n airport-meals get deployments,services,pvc
kubectl -n airport-meals exec deployment/clearing -- cat /opt/airport-meals/INTERFACES.md
kubectl -n airport-meals port-forward service/north 18080:8080
# In a second shell
curl --fail http://127.0.0.1:18080/health
curl --fail http://127.0.0.1:18080/state
```

The API reference lists the individual record reads and write requests for each role. Use the original request ID when retrying a write whose response was interrupted. Read the receiver's recorded receipt, amendment, or close before issuing a new request. Carrier desks can keep accepting booking actions while dispatch is paused.

Each site stores its journal on its own claim. Restart a partner process with `kubectl -n airport-meals rollout restart deployment/<site>` and wait with `kubectl -n airport-meals rollout status deployment/<site>`. Read `/health` and confirm the journal is available before resuming writes.

## Stream releases

The production release publishes to `issue-orders`. The rehearsal publishes to `preview-orders`. Read each route's current release and use the JobManager for its session.

```sh
kubectl -n airport-meals get configmap stream-release-production -o jsonpath='{.data.release\.json}'
kubectl -n airport-meals get configmap stream-release-rehearsal -o jsonpath='{.data.release\.json}'
kubectl -n airport-meals exec deployment/flink-118-jobmanager -- ls /stream-state/archive /stream-state/jars
kubectl -n airport-meals port-forward service/flink-118 18081:8081
```

The archive contains previous release records, job history and reader logs. The same stream volume holds savepoints and the JAR for each supported session. The session template is available at `/opt/airport-meals/flink-session.yaml` in the clearing pod. Set its `__SESSION__` and `__FLINK_IMAGE__` placeholders together. The two pairs are `flink-118` with `flink:1.18.1-scala_2.12-java11`, and `flink-119` with `flink:1.19.0-scala_2.12-java11`. Wait for the JobManager and TaskManager deployments before using the REST API.

Use Flink's `POST /jars/upload` with the session's JAR and `POST /jars/<jar-id>/run` to submit it. The entry class is `airport.meal.MealOrders`. Set `parallelism` and pass the same value in the program arguments. The arguments, in order, are broker address (`kafka:9092`), consumer group, output topic, job name, unique source vertex name, snapshot compression (`true` or `false`), parallelism, checkpoint interval in milliseconds, start mode, and start argument. Start modes are `earliest`, `latest`, `committed`, and `explicit`. The argument is an uppercase reset mode for `committed`, a comma-separated list of `partition:offset` entries for `explicit`, and `NONE` for `earliest` or `latest`. For a restore, pass the savepoint path and ownership mode in `savepointPath` and `restoreMode`.

After a cutover, publish the actual job ID and current settings in the route's `release.json` in `stream-release-<route>`. Use the existing record as the schema. Set `reader` to the source vertex name and `offsets` to the chosen starting vector or null, as appropriate. Record the running JobManager as `jobmanager` (for example, `deployment/flink-119-jobmanager`) and identify the running artifact with either its absolute `jar_path` on that JobManager or its retained Flink `jar_id`. The next shift stops the job with a savepoint and submits it to the existing session at a different parallelism within the available slots.

The Kafka CLI tools are under `/opt/kafka/bin` in `statefulset/kafka`. Desk dispatch and the issuer run separately in `deployment/meal-dispatch` and `deployment/meal-issuer`. Read their operating instructions in `/opt/meal-bridge/INTERFACES.md` in either pod. To trace a live delivery, read the carrier dispatch by action ID, then match the input position and Flink job ID to the committed output and the clearing order keyed by output topic ID, partition and offset.

## Departure and receiver journals

Read the posted statement and departure close for the exact export they used. The carrier publishes the applicable bank and cohort in its own export approvals. Clearing records the admitted orders and their positions in its export receipts. The stream archive holds the original publisher ranges and release history under `/stream-state/archive`. Use the topic ID, writer job ID and exclusive partition intervals together.

For a historical close under a scoped export approval, read the approval and clearing receipt IDs in the close's `contingency_admissions` list. Reconcile that scope using the frozen primary, the linked clearing receipts and signed account changes. Current desk authorizations govern the validity of supplied numbers and current meal allowances.

Read transfer approvals, the carrier adjustment file and its keyed postings before changing a balance. Its ordinary post operation can apply lines still due. Café receipts are immutable. Check the contiguous upload cursor and the signed support batches before submitting a remittance. Use `through_receipt_seq` to pay through a specific receipt when later uploads should stay pending. Local counter records are desk authorizations with their own fulfillment disposition. The partner API reference describes the corresponding reads, posting operations and retry keys.

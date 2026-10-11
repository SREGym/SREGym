# Supplier catalog media expansion

A supplier feed maps rich room content into `roomType.roomDescription` instead of storing an asset URL. The replay imports one normal-looking `BAR` plan for each of the application's 80 hotels. Every imported plan contains a valid inline PNG of approximately 918 kB. Existing `RACK` plans, original document IDs, prices, dates, indexes, credentials, volumes, and service discovery are preserved.

The rate service queries all inventory plans for each nearby hotel and serializes them into a non-expiring Memcached entry. A hotel's JSON entry remains below Memcached's default 1 MiB item limit. However, a normal search considers five hotels, so the aggregated protobuf result exceeds gRPC's default 4 MiB receive limit. Retries also repeat the decoding, sorting, and serialization work. The original 64 MiB cache cannot comfortably hold the expanded catalog. Replay therefore uses a 128 MiB cache and one rate attempt per search to avoid cache churn and retry pressure turning the data incident into recurring process restarts.

The replay includes plausible accompanying capacity tuning: eight CPU cores, `GOMAXPROCS=8`, and a 120-second Memcached operation timeout, a 128 MiB cache, and one search-to-rate attempt. These prevent the application's panic-on-cache-timeout behavior from turning the incident into a pod-startup exercise. They do not change RPC payload limits or remove the bad data. No application code, image, observability component, network object, or helper workload is changed.

## Diagnosis

Follow frontend search traces through search to rate. Search dependency logs expose `ResourceExhausted` and `DeadlineExceeded`; rate queue/attempt metrics and CPU pressure can initially suggest capacity problems. Inspect the rate inventory collection with the Mongo credentials already present in the application's initialization ConfigMap. Compare document sizes and project the room-description length rather than dumping the base64 content:

```javascript
var c = db.getSiblingDB("rate-db").inventory;
c.aggregate([
  {$project:{hotelId:1,code:1,bytes:{$bsonSize:"$$ROOT"},descriptionBytes:{$strLenBytes:"$roomType.roomDescription"}}},
  {$sort:{bytes:-1}}, {$limit:10}
]);
```

`BAR` rows contain unexpectedly large inline image content. The source code deployed inside application containers shows unbounded `Find(...).All`, JSON cache population with no expiration, and the default gRPC client configuration. Check Memcached `stats`, `stats settings`, and a representative cached item's byte count to confirm the second copy of the bad state.

## Repair and reversibility

`mitigate.sh` removes rate inventory rows containing inline base64 image content, drains old rate workers with a rollout, and flushes the shared rate cache. Draining prevents an in-flight read from repopulating obsolete cache entries after invalidation. It reads only live cluster state and uses the application's established MongoDB credentials; it does not use a saved data snapshot or rely on hidden original values. Original inventory rows remain untouched. `recover.sh` performs the same data repair and restores the accompanying rate, search, and cache settings using the deployment's existing last-applied configuration. Derived caches warm naturally.

Clearing Memcached alone cannot repair the source import. Removing MongoDB rows alone cannot repair already cached rate entries. Restarting or rolling back an application deployment preserves both data stores. The scripts use deterministic upserts, are idempotent, wait for Kubernetes rollout completion rather than timing races, and create no helper pods, Jobs, controllers, or cluster markers.

Live validation results are recorded in `validation.json` after completing the end-to-end checks. The SLO endpoint's `new_alerts` field is reported faithfully; logs, application metrics, traces, and MongoDB/cache state provide diagnosis even if that field remains empty.

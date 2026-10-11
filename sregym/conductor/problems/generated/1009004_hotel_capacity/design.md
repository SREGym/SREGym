# Profile admission regression

The incident is a buggy duplicate-work suppression rollout, intended to reduce simultaneous profile cache and database requests. The Go admission interceptor groups requests with identical hotel-ID batches. The admission layer has 64 worker slots and a one-second caller deadline. Slots are released only when the underlying worker finishes, so canceled callers cannot create unbounded background work. Its coalescing waiter holds the batch-map mutex while receiving from the completion channel. The producer finishes its ordinary handler, then needs that mutex to publish its result and close the channel. Overlapping requests create a permanent wait cycle. Because the same mutex guards all batches, the damage spreads to unrelated searches and recommendations. The stuck workers retain their slots; after all 64 slots fill, subsequent RPCs fail with ResourceExhausted. Raising the worker count postpones exhaustion without removing the cycle.

An isolated request works normally. The existing production traffic supplies overlapping hot batches without added traffic, artificial delays, damaged data, or dependency denial. The pod's TCP readiness still passes; CPU and database usage can fall even as users receive errors. Normal request-path logs report admission deadline failures followed by worker capacity exhaustion. This corresponds to a real cache-stampede prevention regression and collapses effective concurrency rather than raw CPU capacity.

The runtime is delivered as a ConfigMap and compiled against the image's vendored Go application at startup. Every restart loads the same source. Deployment history retention is zero, so a routine rollout undo cannot restore a prior template. The image, resources, discovery configuration, databases, and cache contents are unchanged. No helper workloads are created. Scripts remove retired profile registrations because the application's existing registration mechanism has no health checks; stale endpoints are incidental rollout debris and are not part of the intended incident.

An expert follows frontend GetProfiles failures into profile, checks that dependency calls and CPU do not explain the stall, then inspects application goroutines:

```sh
kubectl -n hotel-reservation exec deployment/profile -- curl -s 'http://127.0.0.1:6060/debug/pprof/goroutine?debug=2'
kubectl -n hotel-reservation get configmap profile-runtime -o yaml
```

The stacks show an admission waiter receiving at `batch.ready`, the producer waiting at the publishing mutex, and other requests waiting to acquire that mutex. Disable batch sharing and recreate the pod to clear existing blocked handlers. Alternatively, repair the waiter branch to unlock before receiving and restart. The reference mitigation uses only the live ConfigMap and Kubernetes pod addresses; it needs neither saved original files nor private values. Recovery restores the original `profile` command, removes the runtime volume and readiness addition, returns history retention to its original default, and deletes the runtime ConfigMap.

The validation record is in `validation.jsonl`. It records repeated injection, persistent violated SLO checks, a restart challenge, mitigation, exact template recovery, and final reinjection.

The lock cycle has no autonomous recovery path: caller deadlines return RPC errors but do not close the batch completion channel or release worker slots. The only code that publishes a result first needs the mutex held by the channel waiter. Once all 64 workers are stranded, subsequent requests are rejected without creating more workers, so the process remains stable and degraded indefinitely.

Validation: two checks 70 seconds apart showed all four rounds violated, approximately 99% errors, and new application log surges. Running mitigation twice restored all four rounds to zero errors and roughly 98 RPS. Running recovery twice restored the original Deployment specification exactly and all four rounds met the SLO. Final injection is replayed twice from that recovered state.

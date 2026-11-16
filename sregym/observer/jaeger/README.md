# Central Jaeger capacity

Audience: benchmark operators.

The `observe/jaeger-agent` deployment uses Jaeger all-in-one 1.57, ephemeral
in-memory storage and a 25,000-trace retention cap. Its memory limit is 2 GiB;
requests remain 256 MiB memory and 100m CPU. This adds query headroom while keeping
the existing retention and telemetry interfaces.

A scripted tool-validation control with component monitoring disabled exposed a
reliability risk: Kubernetes recorded `OOMKilled` / exit 137 at
2026-11-16 17:55:48 UTC under the previous 1 GiB limit. The request connection closed,
and the native tool reported its original parsing error. That control retained 159
successful calls and one failed Jaeger call. After the restart, its frozen query
returned zero traces instead of the earlier 545. Ordinary benchmark failure
frequency remains unmeasured.

The 2 GiB limit is a headroom mitigation, with adequacy dependent on workload.
It supplies a finite ceiling rather than a guarantee against OOM. The
[1.57 memory implementation](https://github.com/jaegertracing/jaeger/blob/v1.57.0/plugin/storage/memory/memory.go)
evicts trace IDs by count; spans per trace and their attributes vary. A trace search
copies every matching trace before applying the result-count limit. The
[HTTP query path](https://github.com/jaegertracing/jaeger/blob/v1.57.0/cmd/query/app/http_handler.go)
also constructs response representations. Stored data, ingestion and concurrent
queries therefore share memory, and a small result limit alone leaves allocation
cost variable. Narrow time/service filters reduce matching data.

Applying a changed limit rolls the single-replica deployment. Plan for a brief
query/ingestion interruption and loss of its in-memory traces. Preserve any required
recordings before rollout; identify subsequent data as a new trace epoch. Increasing
this limit leaves storage ephemeral and the retention cap unchanged. Reducing the
cap would shorten retained history; persistent storage requires a separate design.

For each deployment, verify the effective limit, pod/container identity, readiness,
restart count and `memory.current`, `memory.peak` and `memory.events` for its cgroup.
Check representative sequential and concurrent queries while preserving response
counts/sizes and native errors. Memory peaks apply to that container lifetime;
compare counters within the same identity. Validate longer ingestion separately
from short query batches. Bounded successful checks establish only their observed
capacity; larger traces, broader windows or greater concurrency need reassessment.

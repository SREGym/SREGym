# Astronomy memory baseline

Issue: [SREGym #1021](https://github.com/SREGym/SREGym/issues/1021).

Run a no-fault workload on a dedicated kind cluster, with no provider credentials:

```bash
uv sync --locked --python 3.12
# Initialize the applications and Astronomy chart submodules before deployment.
bash kind/setup_kind_cluster.sh x86  # use arm on native ARM64
uv run --no-sync python -m scripts.baseline.run \
  --context kind-kind --seconds 3600 --output /tmp/astronomy-baseline-1
uv run --no-sync python -m scripts.baseline.summarize /tmp/astronomy-baseline-1 \
  > /tmp/astronomy-baseline-1/memory-summary.json
```

The context must also be the current default context: the conductor reads the
normal kubeconfig. Use only a dedicated test cluster. The command refuses to
overwrite evidence or deploy over an existing Astronomy namespace. It archives
the conductor's prior cluster-state cache before recapturing the cluster.
The full profile is the default; `--profile svelte` is a separate qualification.

A fresh deployment needs at least 25 GiB free. Observation stops below 3 GiB free.
Four kind nodes share one host's CPU, RAM, disk, and inotify budget. On the
CloudLab host, `fs.inotify.max_user_instances=128` exhausted during Promtail
startup; raising it to 1024 allowed Promtail to start. Record any host tuning
before a fresh qualification run. This setting does not increase container memory
limits. The CI baseline workflow sets it before deployment.

`--seconds` measures sustained observation after deployment, excluding startup.
Startup has a separate 30-minute timeout. The command retains the cluster and
saves evidence on failure, interruption, and success. `--observe-existing`
collects an explicitly modified experiment without deploying; it cannot establish
a fresh baseline. Run one resource/runtime experiment at a time and save its
exact patch next to the evidence.

## What the artifacts establish

- `environment.json`: source and submodule revisions, tracked working diff,
  Python/kernel/architecture, tool versions, and experiment parameters.
- `initial.json`, `sample-*.json`, `final.json`: all-namespace pod specs and image
  IDs, readiness, init/application termination records, events, metrics, host
  memory/pressure, free disk, and container cgroup current/max/stat/events/swap.
  `memory.peak` is captured when the kernel exposes it. Its absence is not a zero
  peak; a sampled maximum may miss short spikes.
- HTTP samples check the product endpoint and Locust state, rate, and cumulative
  failure ratio. The default maximum failure ratio is 1%; record a changed
  threshold as an experiment parameter.
- `deployment.log`, `kernel.json`, `kind-logs/`, `helm-releases.json`: independent
  evidence even if Loki is down. Failed collection commands retain their errors.
- `memory-summary.json`: sampled current/anonymous/file maxima, container
  identities, and successive five-minute medians. Review the underlying curves;
  a peak truncated by OOM cannot establish a safe replacement limit.

`health_check_passed` means the sampled readiness/traffic/collection checks passed
and no new restarts or OOM were recorded. **It is not memory qualification.**
`qualified` deliberately remains false: qualification requires review of steady
state, three fresh 45–60-minute runs, cleanup/redeployment, the actual native x86
evaluation environment, and relevant fault behavior. Repeated observations of
the same termination are deduplicated by pod UID/container/termination; cgroup
OOM counters provide an additional independent signal. Pod and cgroup records
can describe the same kill; do not add their counts as distinct OOM events.

The **Application Memory Baseline** workflow runs on relevant deployment pull
requests or manual dispatch. It runs three fresh 45-minute x86 jobs and, in the first job, a second 45-minute run after
application cleanup/redeployment. Artifacts are uploaded on every outcome.
Run it when application, monitoring, workload, or deployment dependencies change.
Use the same runner resources as difficulty evaluation; a larger ARM64 host is
useful for diagnosis but cannot qualify that environment.

For local reuse, after retaining the first run's evidence:

```bash
uv run --no-sync python -c \
  'from sregym.service.apps.astronomy_shop import AstronomyShop; AstronomyShop().delete()'
uv run --no-sync python -m scripts.baseline.run \
  --context kind-kind --seconds 3600 --output /tmp/astronomy-redeployed
```

## Runtime changes and provenance

Accounting 2.2.0 retains a single Entity Framework context across every order.
[Upstream #2876](https://github.com/open-telemetry/opentelemetry-demo/pull/2876)
fixes this leak by disposing a context per message. The pinned image is the first
successful upstream nightly after that merge:

- Build: [20939609681](https://github.com/open-telemetry/opentelemetry-demo/actions/runs/20939609681).
- Source: `ebb18186819c510b39d64184d774a527a79e1525`.
- Multi-platform image index:
  `ghcr.io/open-telemetry/demo:nightly-20939609681-accounting@sha256:51e6720438c13d0a4fefd129b01e2422b176fdb18a9ea71ef08fd84e717e82f4`.
- The published AMD64 and ARM64 build provenance both identify that source and
  build. Comparing source against 2.2.0, the only accounting source change is
  the context lifetime fix. Other services stay on their existing images.

Ad and fraud use the explicit maximum heaps from
[upstream #3105](https://github.com/open-telemetry/opentelemetry-demo/pull/3105):
200 MiB and 180 MiB respectively, inside their existing 300 MiB container limits.
The `JAVA_TOOL_OPTIONS` overrides preserve the image's OpenTelemetry Java agent.
The rendered-chart tests cover both profiles, so an ineffective values path or
an override that drops instrumentation fails the regression check.

Loki and Promtail charts are pinned to the measured 7.3.0 and 6.17.1 versions.
Kind images are pinned for both architectures, and metrics-server is pinned to
v0.9.0. The environment and pod records retain the versions/digests actually run,
including other deployment dependencies that are not yet immutable.

## Evaluation attribution

Difficulty evaluation enables `SREGYM_BASELINE_CHECK=1`. Immediately before
injection, an all-namespace snapshot rejects recorded OOMs, unready pods, or a
failed pod collection. The attempt is marked incomplete with `baseline_unhealthy`
or `baseline_diagnostics_failed` and is excluded from difficulty scores.
This is a short pre-injection guard, not a substitute for sustained qualification.
Historical non-OOM restart counts alone do not reject an otherwise ready snapshot.

Snapshots before injection, after injection, and before cleanup are saved under
`results/baseline/`; the result row identifies the directory. Post-injection
findings do not automatically invalidate an attempt: faults and agent actions
may intentionally produce OOMs. Use phase evidence and operation logs to attribute
them. Repeated cleanup snapshots are retained instead of overwriting evidence.
Cluster diagnostics also run when the outer evaluation job succeeds. Provider
and judge failures retain the existing incomplete-evaluation reporting.

## Qualification record

The CloudLab investigation is ARM64, kernel 5.15, about 62 GiB RAM, 8 CPUs,
and 8 GiB host swap. The first run needed an inotify repair and experienced
control-plane lease loss during image pulls. It then hit a hard CPU compatibility
failure: OpenSearch 3.4.0's init image requires ARMv8.2 with cryptographic
extensions; the APM CPU reports only `fp asimd evtstrm cpuid`. Currency 2.2.0
also exits with status 132 (illegal instruction). The run was stopped during
startup, with zero qualified observation time. Evidence is preserved under
`runs/baseline-20260908T222341Z-4j07n8` in the investigation workspace.
This host cannot qualify the full pinned application without changing unrelated
application binaries or emulating a newer CPU. Use a compatible native host.
The live collector smoke test rejected the unhealthy cluster and retained
artifacts: product requests returned 200, but Locust reported a 50% cumulative
failure ratio. The candidate image digest and Java options were verified in
live Deployment specs; that is configuration validation, not workload qualification.

Sustained before/after measurements, Loki's demand classification, Kafka diagnostic
process overhead ([#1015](https://github.com/SREGym/SREGym/issues/1015)), three
fresh runs, and native x86 qualification must be recorded before closing #1021.

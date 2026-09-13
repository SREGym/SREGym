# Blueprint Hotel images

The nine `blueprint-hotel-*` build targets publish to the neutral package
`ghcr.io/sregym/blueprint-hotel`, with one release tag per service. Deployment
references in `SREGym-applications/BlueprintHotelReservation` and
`docker/images.lock.json` must point to the same multiarch index digests.

## Source and compatibility

The original `777lefty` images contain only Linux AMD64 executables. Their
customized source was not available in the public repositories we inspected.
The port therefore uses two explicit paths:

- **AMD64:** preserve the original image and executable, pinned by digest in
  `docker/images.hcl`; add the upstream Blueprint license.
- **ARM64:** compile the public [Blueprint source at
  `15c2f0c24ab6c51f08cd625644feb09af33a7c9a`](https://github.com/jq-huang/blueprint/tree/15c2f0c24ab6c51f08cd625644feb09af33a7c9a),
  with `compatibility.patch` restoring the observed benchmark controls.

The archive checksum, Go toolchains, protobuf generators, runtime base images,
and service dependency versions are pinned. The services use Go 1.23.12 and
the workload uses Go 1.25.0, matching their original executable metadata.
`service-dependencies.txt` records the union of module versions recovered with
`go version -m` from the eight original service executables; `go mod tidy`
retains the dependencies needed by each generated service. ARM executables are
statically compiled for `linux/arm64`, without emulating AMD64 at runtime.

The compatibility patch is a reconstruction, not the author's missing source.
It leaves the public application's business logic intact and restores:

- `GRPC_CLIENT_TIMEOUT` (default `1s`) and
  `GRPC_CLIENT_RETRIES_ON_ERROR` (default `1`). The latter counts **total
  attempts**, not additional retries. Each attempt receives a fresh timeout
  from the caller's context; a successful response stops the loop.
- The workload's base/spike/base traffic schedule and its `multiplier`,
  `stabletime`, `triggertime`, and `reverttime` flags. Defaults are 3,000
  requests/second, multiplier 6, phases 60/30/30 seconds, and duration 120s.
  As in the original, `duration` stops the run; `reverttime` is metadata, not
  an independent termination condition.
- The workload start marker, per-request CSV, and per-second latency/load
  output consumed by the benchmark. The historical two-column CSV header
  above three-column data rows is deliberately retained.

Do not silently substitute the unpatched upstream compiler: ordinary HTTP
requests can work while the retry-storm and workload-spike controls disappear.
Likewise, changing these dependency versions or retry semantics is a benchmark
behavior change, not merely a packaging update. The reconstruction guards the
progress-log interval against division by zero for very small smoke workloads.

## Build and release

From the repository root, with Buildx and a registry login:

```sh
IMAGE_TAG=YYYYMMDD docker buildx bake -f docker/images.hcl blueprint-hotel --push
```

This builds both `linux/amd64` and `linux/arm64`. To build just one service,
select its target, such as `blueprint-hotel-search`. `ORIGINAL_IMAGE` is
required and supplied by Bake; the fixed AMD64 original stage is intentional.
Direct Dockerfile builds without those arguments are not supported.

Inspect the published indexes, record their digests in the lock file, and
update all ten consumers (eight services plus the workload Deployment and
Job). New GHCR packages also need **Public** visibility for credential-free
cluster pulls; successful authenticated publication alone does not prove that.
There is no publishing workflow associated with these recipes.

## Verification

Pull the exact locked reference for each architecture before running the
shared smoke entry point with the project's Python environment on `PATH`:

```sh
docker pull --platform linux/arm64 IMAGE
PATH="$PWD/.venv/bin:$PATH" bash docker/test_image.sh blueprint-hotel-search IMAGE arm64
```

Repeat for every service and both architectures. The checks inspect the actual
ELF machine type and run each executable. Frontend/search tests use disposable
gRPC fixtures to check default attempts, repeated failures, the benchmark's
30-attempt/50ms settings, fresh deadlines, and recovery after two failures.
The workload test checks actual request timestamps across all three phases and
recomputes the per-second latency averages. These same checks can run against
the digest-pinned original images as differential tests. Use a native ARM
Docker host for native-execution evidence; emulated AMD64 tests establish
compatibility, not native AMD64 performance.

For integration verification, deploy the manifests to a disposable ARM cluster,
wait for all services and dependencies to become ready, and forward the
frontend's service port. Then run:

```sh
uv run python docker/blueprint-hotel/test_app.py http://127.0.0.1:PORT
uv run python docker/check_image_platforms.py \
  --manifest SREGym-applications/BlueprintHotelReservation
uv run pytest tests/docker/test_blueprint_images.py
```

The application check exercises search, authentication, three recommendation
criteria, and reservation. It writes a reservation using a synthetic built-in
user, so do not target a benchmark database that must be preserved. Also run a
bounded workload against that deployment and remove the disposable resources
afterward. These tests cover image execution and the restored controls; they
do not establish full equivalence of every long-running metastability fault.

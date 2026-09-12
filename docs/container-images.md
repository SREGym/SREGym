# Container images

Published SREGym images use multiarch index digests. Docker and Kubernetes select
the native AMD64 or ARM64 image from the same reference. Normal runs pull these
images without a local build or registry login.

## Compatibility

SREGym-Lite supports AMD64 and ARM64. On macOS, containers run inside the Linux VM
provided by Docker Desktop or OrbStack. See the [KIND guide](../kind/README.md)
for setup and resource requirements.

Outside Lite, Blueprint's custom images and the modified kube-proxy used by
`workload_imbalance` remain AMD64-only. FlightTicket requires a configured
OpenWhisk installation. TrainTicket requires more memory than Lite. Native ARM
component checks do not establish full application support for these workloads.

Compatibility builds preserve existing application and dependency versions,
including legacy databases. They are not production security upgrades. Percona's
ARM image retains InnoDB and the required replication plugins, but omits the
x86-only TokuDB engine. TrainTicket does not enable that engine.

## Build an image

Run these commands from the repository root with all submodules initialized:

```bash
docker buildx bake -f docker/images.hcl media-frontend \
  --set '*.platform=linux/arm64' --load
bash docker/test_image.sh media-frontend ghcr.io/sregym/media-frontend:local arm64
```

Use `linux/amd64` and `amd64` for an AMD64 build. The build targets and contexts
are in [docker/images.hcl](../docker/images.hcl).

For an optional local agent rebuild, run `bash docker/agents/build.sh`. It uses
the host kubectl version when available. `KUBECTL_VERSION` overrides that value.
The published agent image uses the bundled KIND version.

## Publish and update releases

Authenticate Docker to GHCR with package-write permission. Use an unused release
tag and a builder that supports both platforms:

```bash
IMAGE_TAG=YYYYMMDD REVISION="$(git rev-parse HEAD)" \
  docker buildx bake -f docker/images.hcl --push publish
```

Keep Hotel variant tags neutral, as described in their
[build guide](../docker/hotel-reservation/README.md).

Publish helper images first. Record their index digests in
[docker/images.lock.json](../docker/images.lock.json), then synchronize consumers:

```bash
uv run python docker/sync_images.py
uv run python docker/sync_images.py --check
```

Rebuild dependent images after synchronization. The TrainTicket installer embeds
helper references, and the FlightTicket action-deployer uses the Python runtime
for both packaging and execution. Record and synchronize those releases too.
Commit changed submodules before updating their parent references.

Make new GHCR packages public for anonymous cluster pulls. Check both platforms
and run the image smoke checks before updating deployed releases:

```bash
uv run python docker/check_image_platforms.py $(jq -r '.[]' docker/images.lock.json)
bash docker/test_image.sh IMAGE_TARGET IMAGE_REFERENCE arm64
bash docker/test_image.sh IMAGE_TARGET IMAGE_REFERENCE amd64
```

Manifest checks establish platform availability, not application health. See
[local validation](../tests/integration/README.md) for lifecycle and agent checks.

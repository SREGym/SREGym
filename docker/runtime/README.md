# Offline runtime images

These images contain packages that application helpers previously downloaded at startup.
The Social Network image contains the Lua files and pages from the application submodule.
Pod startup and replacement do not need PyPI, package mirrors, or GitHub source downloads.

| Image | Contents | Consumers |
| -- | -- | -- |
| `ghcr.io/sregym/redis-client:8.1.0-py3.10` | Python 3.10 and redis 8.1.0 | Valkey memory helper |
| `ghcr.io/sregym/kafka-client:2.5.3-py3.12` | Python 3.12 and confluent-kafka 2.5.3 | Kafka producer and validator helpers |
| `ghcr.io/sregym/tls-client:ubuntu22.04` | Ubuntu 22.04, OpenSSL, and CA certificates | TLS verification sidecar |
| `ghcr.io/sregym/social-network-assets:v1` | Lua files, generated Thrift files, pages, and existing test certificates | Social Network init containers |
| `ghcr.io/sregym/grafana:12.3.1-opensearch2.34.3` | Grafana 12.3.1 and the signed OpenSearch plugin 2.34.3 | Astronomy Shop Grafana |

## Current publication status

All five packages are public. Each tag in the table contains `linux/amd64` and `linux/arm64` images.
Offline checks passed on native AMD64. The Redis, Kafka, TLS, and Social Network
images also passed ARM64 checks through Docker BuildKit's existing emulator.
The Grafana ARM64 image builds successfully; its runtime check remains deferred.
Native ARM64 and full macOS benchmark testing remain separate checks.

## Architecture support

Each published release tag must contain both `linux/amd64` and `linux/arm64` images.
Docker selects the image that matches the node architecture.
Apple Silicon Macs use the ARM64 image through their Linux container runtime, such as Docker Desktop.
Intel Macs and x86 Linux nodes use the AMD64 image.

Builds run manually with Docker. No GitHub Actions workflow is required.
Docker Buildx can use its builder's bundled emulator to build ARM64 images on an AMD64 host.
This emulator is a build tool, not a SREGym runtime dependency.
These image checks do not replace a full benchmark test on macOS.

## Build

Run these commands from the repository root:

```bash
docker build -t ghcr.io/sregym/redis-client:8.1.0-py3.10 docker/runtime/redis
docker build -t ghcr.io/sregym/kafka-client:2.5.3-py3.12 docker/runtime/kafka
docker build -t ghcr.io/sregym/tls-client:ubuntu22.04 docker/runtime/tls
docker build -t ghcr.io/sregym/grafana:12.3.1-opensearch2.34.3 docker/runtime/grafana
docker build -t ghcr.io/sregym/social-network-assets:v1 \
  -f SREGym-applications/socialNetwork/Dockerfile-assets \
  SREGym-applications/socialNetwork
```

Build on the architecture of the target nodes. These builds use Docker and require internet access only during the build.
These commands create single-architecture development images, not release tags for publication.

For AMD64 images, run the offline startup checks:

```bash
bash docker/runtime/smoke-test.sh redis ghcr.io/sregym/redis-client:8.1.0-py3.10 amd64
bash docker/runtime/smoke-test.sh kafka ghcr.io/sregym/kafka-client:2.5.3-py3.12 amd64
bash docker/runtime/smoke-test.sh tls ghcr.io/sregym/tls-client:ubuntu22.04 amd64
bash docker/runtime/smoke-test.sh grafana ghcr.io/sregym/grafana:12.3.1-opensearch2.34.3 amd64
bash SREGym-applications/socialNetwork/test-assets-image.sh ghcr.io/sregym/social-network-assets:v1 amd64
```

For ARM64 images, replace `amd64` with `arm64`.
Each check verifies the image architecture and runs the helper with networking disabled.

To build an ARM64 image with an existing cross-platform builder, specify the target platform:

```bash
docker buildx build --platform linux/arm64 --provenance=false --push \
  --tag ghcr.io/sregym/redis-client:8.1.0-py3.10-arm64 docker/runtime/redis
```

Use the corresponding image tag and Dockerfile context for the other images.
If the builder cannot execute ARM64 commands, use an ARM64 machine for the build.

## Load into a test cluster

For tests without registry access, load all five images into **every node** before a run.
A replacement pod can use a different node.

For kind, run this command on the host of the kind cluster:

```bash
kind load docker-image --name kind \
  ghcr.io/sregym/redis-client:8.1.0-py3.10 \
  ghcr.io/sregym/kafka-client:2.5.3-py3.12 \
  ghcr.io/sregym/tls-client:ubuntu22.04 \
  ghcr.io/sregym/social-network-assets:v1 \
  ghcr.io/sregym/grafana:12.3.1-opensearch2.34.3
```

For a remote cluster, export the images:

```bash
docker save -o runtime-images.tar \
  ghcr.io/sregym/redis-client:8.1.0-py3.10 \
  ghcr.io/sregym/kafka-client:2.5.3-py3.12 \
  ghcr.io/sregym/tls-client:ubuntu22.04 \
  ghcr.io/sregym/social-network-assets:v1 \
  ghcr.io/sregym/grafana:12.3.1-opensearch2.34.3
```

Copy the archive to every node. Import it with the existing container runtime.
For Docker, use `sudo docker load -i runtime-images.tar`.
For containerd, use `sudo ctr -n k8s.io images import runtime-images.tar`.

## Publish a new version

Build each image for AMD64 and ARM64.
Run the offline checks for each architecture.
Record whether each check used native hardware or emulation.
Authenticate to `ghcr.io` with a package-write token.
Do not put credentials in a Dockerfile, build argument, or image layer.

Push the tested images with separate `<version>-amd64` and `<version>-arm64` tags.
After both architecture tests pass, combine their tags into one release tag:

```bash
docker buildx imagetools create --tag 'ghcr.io/sregym/<image>:<version>' \
  'ghcr.io/sregym/<image>:<version>-amd64' \
  'ghcr.io/sregym/<image>:<version>-arm64'
docker buildx imagetools inspect 'ghcr.io/sregym/<image>:<version>'
```

Verify that the release tag contains both `linux/amd64` and `linux/arm64`.
New GitHub packages can default to internal visibility. Make each package public and verify an anonymous pull before use.

Use a new tag for changed content.
Update `sregym/service/runtime_images.py` and the affected Helm values to match.
Publish the application submodule changes and update its parent reference before release.

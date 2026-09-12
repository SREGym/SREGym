# Container images

SREGym targets both `linux/amd64` and `linux/arm64`. Published replacements
pin the **multiarch index digest**, not a single-architecture image digest.
One reference works on both architectures. This covers the whole application
catalog, not only SREGym-Lite. The migration is not yet complete: Blueprint's
custom source and the workload-imbalance kube-proxy patch are missing, and some
full-application validations need additional infrastructure. See the
[validation report](macOS-multiarch-validation.md).

The SREGym-maintained releases are recorded in
[`docker/images.lock.json`](../docker/images.lock.json): Hotel
Reservation, Social Network services and dependency base, its two frontends,
wrk2, the Locust exporter, the KIND node, the agent runtime, Fleet Cast, and
FlightTicket/TrainTicket helpers, two Hotel fault images, and stress. Selected upstream replacements are recorded
there too; other already-multiarch upstream dependencies remain in use.

The Hotel build uses `ghcr.io/sregym/hotel-reservation`; historical tags are
left unchanged. The earlier `lite-hotel-reservation` package is no longer used.

## Compatibility and source preservation

- Fleet Cast keeps the vendored backend and the original image's Python package
  versions. Its frontend and TiDB images already support both architectures.
- FlightTicket keeps its Python 3.6 action runtime, proxy code and dependency
  versions, rebuilding native extensions for ARM64. Its action-deployer uses
  that published runtime for both packaging and execution. OpenWhisk itself
  must be configured separately; the existing job expects a Docker socket.
- TrainTicket retains the installer's existing application service images and
  scripts. `docker/train-ticket/pin_images.py` updates only the embedded
  database/helper references. Percona 5.7.34, MySQL 5.7.36 and Xenon's exact
  Kubernetes-specific source revision have native ARM64 build recipes. Nacos
  retains its complete distribution and Java 8u292; RabbitMQ uses the official
  multiarch 3.8.19 image. The old MySQL exporter version is preserved because
  the chart uses its `DATA_SOURCE_NAME` interface.
- The Percona ARM64 build omits the x86-only TokuDB engine and unused RocksDB
  engine. TrainTicket's chart uses InnoDB with `initTokudb: false`; its semi-sync
  and audit plugins are retained. This is not a claim of ARM support for every
  optional Percona engine.
- Both legacy MySQL images cap an excessively large inherited file-descriptor
  soft limit at 655360, preserving smaller limits. This prevents MySQL 5.7 from
  allocating gigabytes of descriptor bookkeeping under modern containerd,
  on both architectures. Database settings and chart memory limits stay intact.
- Blueprint's nine `777lefty` images cannot be faithfully rebuilt without their
  modified source. Substituting public upstream code would change the faults.
- Hotel's Geo fault image retains its original source, vendored dependencies,
  Go version, and incorrect MongoDB port. The correlated fault image preserves
  the original wrong-application rollout: Social Network binaries with none of
  Hotel's eight startup commands. See the
  [image recipes and provenance](../docker/hotel-reservation/README.md).
  Both variants use the public `hotel-reservation` package and neutral numbered
  release tags; their names do not reveal the injected fault.
- The CPU-stress helper retains stress 1.0.4 and its existing invocation.
- `incorrect_image` deliberately injects a nonexistent image; that tag must
  remain nonexistent. Recovery now restores the exact pre-injection reference
  and container name, rather than guessing a hardcoded release. Its snapshot
  belongs to the problem instance and rejects a recreated Deployment.

These preserve benchmark versions, including legacy/EOL dependencies. They are
compatibility images for isolated benchmark environments, not a production
security upgrade.

## Optional local builds

Run from the repository root with the applications submodule initialized.
[`docker/images.hcl`](../docker/images.hcl) holds the build contexts,
tags, and Social Network dependency relationship. For example:

```bash
# Build one image natively on an ARM64 workstation for testing.
docker buildx bake -f docker/images.hcl media-frontend \
  --set '*.platform=linux/arm64' --load
bash docker/test_image.sh media-frontend ghcr.io/sregym/media-frontend:local arm64
```

To publish a new version locally, authenticate Docker to GHCR with an account
authorized to write the packages. A GitHub CLI login alone does not guarantee
`write:packages` permission. Select an unused date-based tag (keeping the Hotel
variants' names neutral) and use a Buildx builder that supports both platforms:

```bash
IMAGE_TAG=20260913 REVISION="$(git rev-parse HEAD)" \
  docker buildx bake -f docker/images.hcl --push publish
```

The C++ builds can be slow under emulation. Run startup checks on both
architectures before updating the deployed digests. Publishing a new tag does
not automatically change the pinned deployments. No GitHub workflow is needed.

Publish helper images first, record their verified index digests, then rebuild
the TrainTicket deployer so its embedded references use the new lock entries.
The FlightTicket action-deployer's `PYTHON_RUNTIME_IMAGE` build argument must
likewise point to the verified runtime release. Tests check these relationships.

New packages may default to internal visibility. They must become public for
credential-free Kubernetes pulls, or the deployment needs explicit registry
credentials. Local KIND validation can load the verified images instead;
that does not establish anonymous registry access.

### Package visibility

On September 12, all 21 previously maintained packages were public, including
the 11 FleetCast/FlightTicket/TrainTicket packages that were internal on
September 11. Both platform manifests were accessible anonymously.

The Hotel variants use the existing public `hotel-reservation` package with
tags `20260912.1` and `20260912.2`; they do not require registry credentials.
The new `stress` package still needs public visibility for credential-free
pulls. GitHub defaults new packages to internal; publishing and multiarch
support do not establish public access.

## Verification

Check the recorded releases against the registry:

```bash
uv run python docker/check_image_platforms.py $(jq -r '.[]' docker/images.lock.json)

# Include fault/helper images pulled after deployment and Blueprint's manifests.
# This still fails for the known source-blocked kube-proxy and Blueprint images.
uv run python docker/check_image_platforms.py \
  $(uv run python -c 'from sregym.generators import images; print(" ".join(v for k, v in vars(images).items() if k.endswith("_IMAGE")))') \
  --manifest SREGym-applications/BlueprintHotelReservation
```

The checker also accepts `--manifest path.yaml` for rendered Kubernetes YAML
and includes sidecars and init containers. A successful manifest check proves
architecture availability; application health and fault behavior require the
[problem lifecycle checks](SREGym-Lite.md#validate-a-local-installation-without-model-calls)
and application-specific validation. The current results and limits are in the
[Apple silicon validation report](macOS-multiarch-validation.md).

The agent pulls its published image by default. The explicit agent rebuild
option builds local source and switches that run to `sregym-agent-base:latest`.

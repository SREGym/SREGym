# Multiarch validation on Apple silicon

Validation on September 11, 2026, on an Apple silicon Mac using OrbStack
(15 Linux CPUs, 16 GiB VM memory). The disposable four-node KIND cluster ran
Kubernetes 1.32.1 on ARM64, with Calico and the full shared monitoring stack.
It used a separate kubeconfig and baseline file; the existing user cluster
and its saved baseline were not modified.

This report supplements the [September 4 validation](macOS-Lite-validation.md).
Those historical 21-problem results are not presented as a new full-suite run.
The current image catalog and publishing instructions are in
[Container images](container-images.md).

## Verified application paths

| Application or component | Current checks |
|---|---|
| Hotel Reservation | Published-image full lifecycle: `service_wrong_pod_selection_hotel_reservation` passed |
| Social Network | Published-image full lifecycle: `wrong_service_selector_social_network` passed |
| Astronomy Shop | Published-image full lifecycle: `wrong_dns_policy_astronomy_shop` passed |
| Fleet Cast / TiDB | Native deployment, initialized database, advancing telemetry, dashboard and station queries, frontend and API through ingress |
| Fleet Cast operator fault | Wrong-update-strategy injection made the real oracle fail; recovery made it pass, preserving the original TiDB version, complete spec and resource UID |
| Agent runtime | Five CLI bootstrap tests and two open/filtered connectivity tests passed with the published image |
| FlightTicket Python runtime | Real ZIP-action `/init` and `/run` requests passed on ARM64 and AMD64 |

The Fleet Cast rebuild preserves the original backend's Python dependency
versions. An initial unpinned rebuild upgraded PyMySQL and broke database
requests despite Ready pods; the corrected image passes the API checks above.

The operator injectors now change only the intended misconfiguration. They no
longer downgrade TiDB to 3.0.8 or replace the original configuration with a
generic recovery example. Recovery snapshots survive a new injector instance,
are scoped by namespace UID, and reject stale resource UIDs.

## TrainTicket component checks

The actual deployment job embeds its own charts and manifests. Its 46 service
images already offer AMD64 and ARM64; the remaining image work is in its
database/helper images. Updating the separate legacy manifests would not
change what that job deploys.

Native checks exercised Nacos 2.0.1 configuration read/write and service
registration, including startup in cluster mode against the rebuilt Percona
database. The original Nacos schema initialization script completed using the
rebuilt MariaDB client. Xenon reached LEADER with an ALIVE/READWRITE database;
the MySQL exporter reported `mysql_up 1`. Percona loaded InnoDB, both semi-sync
plugins and the audit plugin. Alertsnitch's MySQL 5.7.36 initialization created
all eight application tables and accepted SQL writes through the default
client socket. RabbitMQ 3.8.19 started with its Prometheus plugin.

The final published Percona/Xenon images also passed the actual three-replica
MySQL chart on ARM KIND, retaining the default resources and configuration
(with its optional metrics sidecar enabled). After the installer's existing
IPv6-root grant and Xenon restart, one node became leader and two followers
were ALIVE/READONLY. A write through the leader Service replicated to all three
nodes; GTID waits completed and every exporter reported `mysql_up 1`.

This caught a containerd compatibility issue in both the original AMD64 and
rebuilt ARM64 MySQL binaries: a billion-descriptor inherited limit caused an
OOM before initialization. Both MySQL images now cap only excessive soft limits
at 655360. Their high-limit smoke checks pass within 1 GiB on both architectures;
the database chart's memory limits and settings were not changed.

The published installer passed CLI and embedded-chart checks on both
architectures. Its seven helper references match the lock file and its original
deployment script is unchanged. All **60 active image references**, including
monitoring and the generated service deployment template, passed the registry
architecture audit. Inactive legacy example manifests are not deployed by that
installer and are not claimed to be migrated.

These are component checks, not a completed TrainTicket booking workflow.
The rendered default services plus two database clusters and Nacos request
**18.53 GiB**, before RabbitMQ, Locust, monitoring and KIND. That exceeds this
Mac's 16 GiB Linux VM allocation. A full default deployment needs a larger ARM
test environment; reducing the benchmark's defaults was not part of this work.

## Regression and image checks

- Broad non-live suite: **1,421 passed, 1 skipped**. The skipped fixture-dependent
  trace test and excluded live/model suites are not counted as passes.
- Image reference tests check deployed pins, recovery images, Helm value files,
  sidecar preservation, and agent pull/rebuild behavior.
- TrainTicket build-time tests verify that only four embedded configuration
  files change and that every non-image value remains unchanged.
- Operator tests cover all five targeted field changes, repeated injection,
  recovery through a new instance, namespace isolation, stale UIDs, and retry
  after a failed storage recreation.
- Registry checks inspect published multiarch indexes, including init containers
  and sidecars. All 22 locked references passed (21 maintained images and the
  selected upstream RabbitMQ release). Local ARM64 execution is native; AMD64 startup checks on this
  Mac use emulation and are not called native AMD64 tests.

Commands for the broad suite and individual image checks:

```bash
uv run pytest tests --ignore=tests/integration \
  --ignore=tests/e2e-testing-scripts --ignore=tests/file_editing \
  --ignore=tests/kubectl_tool_tests \
  --ignore=tests/llm_as_a_judge/test_stratus_rejudge.py -q

uv run python docker/check_image_platforms.py $(jq -r '.[]' docker/images.lock.json)
bash docker/test_image.sh IMAGE_TARGET IMAGE_REFERENCE arm64
bash docker/test_image.sh IMAGE_TARGET IMAGE_REFERENCE amd64
```

Raw local evidence is under `.runtime/multiarch-validation/` and task-specific
`/tmp/sregym-*.log` build/test logs. These generated files are not committed.

## Remaining boundaries

- **Blueprint Hotel Reservation:** its nine custom `777lefty` images are still
  AMD64-only. The repository contains manifests, not their modified source.
  Public Blueprint source does not implement the same retry/fault behavior.
  The matching source is required to produce faithful ARM64 replacements.
- **FlightTicket deployment:** its job images and Python action runtime have
  multiarch builds, but a configured OpenWhisk installation is an external
  prerequisite. The existing packaging job also expects a Docker socket.
  No full OpenWhisk/FlightTicket deployment was tested, and the external example
  endpoint in the upstream chart was not used or modified.
- **Package visibility:** new GHCR packages remain internal at the user's
  request. Internal-image KIND tests use the exact verified image bytes loaded
  into the disposable nodes, with test-only pull-policy adjustments where
  needed. Anonymous pulls cannot work until those packages become public.
- Hardware/Khaos requirements are separate from container architecture;
  multiarch images do not make bare-metal-only faults runnable inside KIND.

# GitLab CE, Mattermost and SWE-Marathon Stripe prototypes

These opt-in applications extend the existing DinD benchmark. Each has `single`
(one PostgreSQL member) and `replicated` (three members, one required synchronous
standby) tiers, retained state, a real API workflow and a state-aware oracle.
The application tier stays at one instance. These are executable foundations
for future incident families; adding replicas is not evidence of higher agent
difficulty.

See the [admission results](saas-prototype-results.md) for live validation,
upstream contract tests, image identities and limitations.

| Application | Runtime | Retained state | Business probe |
|---|---|---|---|
| GitLab CE | GitLab CE 18.11.12, external PostgreSQL 16.14 and Redis 7.4.2 | Git repositories, GitLab configuration/secrets, issues and Redis AOF | Create/read an issue and a committed Git file |
| Mattermost | Team Edition 11.7.0, external PostgreSQL 16.14 | Team/channel/messages and uploaded attachments | Log in, post a message, upload/download its attachment |
| SWE-Marathon Stripe | Pinned reference API, transactional PostgreSQL adapter, separate billing/webhook worker | Customers, payments, refunds, idempotency responses, webhook jobs and receiver receipts | Capture a test payment, replay its key, refund part of it and verify webhook delivery |

All PostgreSQL members use separate PVCs and the existing pinned CloudNativePG
1.30.1 operator. Ordinary redeployment does not reseed an existing application.
The small disposable datasets use a 30-second smart-shutdown window within a
90-second total PostgreSQL shutdown budget, so teardown fits the namespace
cleanup timeout. These are explicit prototype settings.
Secrets are created per deployment and passed through Kubernetes Secrets; the
workflows do not use external SaaS accounts or real payments.

The prototype oracle preserves the identities and contents of captured synthetic business
transactions and their storage. It does not audit every application table or
every user permission. The separate [GitLab and Stripe postmortem families](saas-postmortems.md)
add incident-specific inventories, negative controls and explicit recovery tails.

## Reproduction

Build the complete DinD image from the checkout:

```bash
python3 docker/dind/run.py build --image sregym-dind:saas
python3 docker/dind/run.py run --image sregym-dind:saas \
  --name saas-prototypes --cpus 8 --memory 34g --docker-tmpfs-size 24g
```

In another terminal, wait for readiness and prepare the operator and Stripe image:

```bash
docker exec saas-prototypes test -f /run/sregym-ready
docker exec saas-prototypes python scripts/prepare_saas_prototypes.py
```

Preparation verifies the imported upstream hashes, builds the adapter with locked
Python dependencies, and loads its content-addressed local image into KIND.
GitLab and Mattermost use version-pinned published images. Keep runtime image
digests from each validation report when comparing runs.

Run the admission tests serially for each application and tier:

```bash
docker exec saas-prototypes python tests/integration/validate_saas.py \
  --application gitlab_ce --tier replicated --output results/gitlab-ce-replicated.json
docker exec saas-prototypes python tests/integration/validate_saas.py \
  --application mattermost --tier replicated --output results/mattermost-replicated.json
docker exec saas-prototypes python tests/integration/validate_saas.py \
  --application stripe_marathon --tier replicated --output results/stripe-replicated.json
```

Use `--tier single` for the corresponding smaller environment. Each test requires
healthy → selector fault detected → reference repair healthy, rejects deliberate
corruption of an acknowledged record, and verifies application restart durability
and original volume identities. Replicated tests also switch PostgreSQL primary
and replace a database pod. Stripe additionally queues a payment webhook while
the worker is stopped, restarts the API/worker/receiver, and verifies delivery and
retained payment idempotency. Finally, namespace cleanup must reclaim its volumes.

The regular conductor lifecycle validator accepts these six IDs:

```text
wrong_service_selector_gitlab_ce_single
wrong_service_selector_gitlab_ce_replicated
wrong_service_selector_mattermost_single
wrong_service_selector_mattermost_replicated
wrong_service_selector_stripe_marathon_single
wrong_service_selector_stripe_marathon_replicated
```

For example:

```bash
docker exec saas-prototypes python tests/integration/validate_problem.py \
  --problem wrong_service_selector_mattermost_replicated --profile svelte \
  --summary results/mattermost-lifecycle.md --json-summary results/mattermost-lifecycle.json
```

The comparison runner also accepts `--applications gitlab_ce mattermost
stripe_marathon`. Model evaluations are separate from prototype admission; these
prototypes alone do not establish new difficulty scores.

Run the original Stripe contract tests against both versions:

```bash
docker exec saas-prototypes python tests/integration/validate_stripe_contracts.py \
  --output results/stripe-contracts
```

This runs all twelve unchanged test modules without retrying failed tests. It
uses Stripe SDK 10.10.0, pytest 8.4.1 and the upstream accelerated billing/retry
settings. The durable API and worker run in separate containers sharing a network
namespace so the original localhost webhook receivers work unchanged. In normal
SREGym deployments they run in separate Kubernetes pods. All contract containers
and their isolated network are removed after the run.

Stop the private environment when finished:

```bash
docker stop saas-prototypes
```

## Source reuse and fidelity

The imported Stripe code is the **human-written reference solution**, not the
source of an agent's successful submission. It comes from SWE-Marathon revision
`5c468fae8656ef9f7bca36cc8c6ee6e7478aa0f6`. Its Apache-2.0 license, unchanged
source/tests, notice and SHA-256 manifest are included under
`sregym/service/apps/fixtures/swe-marathon-stripe/`.
[Upstream task and source](https://github.com/abundant-ai/swe-marathon/tree/5c468fae8656ef9f7bca36cc8c6ee6e7478aa0f6/tasks/stripe-clone)

The Stripe reference originally stores state in process-local dictionaries.
`docker/stripe-marathon/backend.py` preserves that object model in one PostgreSQL
JSONB document. A row lock serializes updates; business effects and the
idempotency response commit before HTTP acknowledgement. Pending webhook work
commits with its event, and a separate worker leases deliveries and resumes
expired leases after a crash. Delivery is at least once; the local receiver
deduplicates by event ID. It does not simulate a card network or financial risk.
The document/lock is a deliberate throughput limit, not a production payment
schema. No claim of scalable concurrent app replicas is made.

GitLab's prototype uses the CE Linux-package container with external PostgreSQL
and Redis. Puma, Sidekiq and Gitaly share one application pod. GitLab recommends
its chart/operator for Kubernetes production deployments; this small prototype
does not claim that topology's availability properties. Persistent configuration
keeps encryption secrets and database/repository state together across restart.
[GitLab container guidance](https://docs.gitlab.com/install/docker/installation/),
[external database configuration](https://docs.gitlab.com/omnibus/settings/database/)

Mattermost uses its published Team Edition image with one server and local
persistent attachments. Replicated PostgreSQL does not make the application or
attachment storage highly available. The published image and source have distinct
license terms; no Enterprise HA functionality is assumed.
[Container configuration](https://github.com/mattermost/docker),
[pinned licensing](https://github.com/mattermost/mattermost/blob/v11.7.0/LICENSE.txt)

All nodes share one physical machine. Memory-backed Docker storage verifies
process/pod restart persistence, not survival of host power loss or realistic disk
latency. The memory option also mounts DinD's `/tmp` in tmpfs so KIND image-export
archives do not fill the host root disk; temporary files count against the outer
memory limit. The configured 34 GiB/8 CPU budget is a validation setting, not a
measured minimum. Run the large applications serially.

The fault described on this page is service-selector corruption, with preserved
business-state invariants. That is the lifecycle fault these prototypes ship with;
the incident families built on top of them live in their own documents:

- GitLab 2017 deletion shape: [database deletion](saas-postmortems.md) and its
  [notification](gitlab-notification-recovery.md) variants.
- GitHub 2018 failover shape: [regional failover divergence](gitlab-regional-failover.md).
- Slack 2021 cascade shape: [Mattermost capacity cascade](mattermost-capacity-cascade.md).
- Cloudflare 2025 configuration shape: [recurring bad configuration](stripe-config-screen.md).

Each reproduces the causal shape of its postmortem, not the incident itself; every
family's document states what it does and does not model.

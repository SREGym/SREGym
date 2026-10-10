# SaaS prototype admission results

All **six application/tier admissions passed**: GitLab CE, Mattermost and
SWE-Marathon Stripe, each with one-member and three-member PostgreSQL tiers.
The ordinary SREGym conductor lifecycle also passed all eight stages for
Mattermost single. These are prototype validation results; no Codex attempts
were run for these three applications and no difficulty increase is established.
The six extended admissions exercise the problem class directly; the additional
Mattermost run uses the standard conductor with the `svelte` profile and no Loki.

| Application | Tier | Admission | Original volumes retained |
|---|---|---|---:|
| GitLab CE | single | pass | 4 |
| GitLab CE | replicated | pass | 6 |
| Mattermost | single | pass | 2 |
| Mattermost | replicated | pass | 4 |
| SWE-Marathon Stripe | single | pass | 2 |
| SWE-Marathon Stripe | replicated | pass | 4 |

Every case passed healthy operation, selector-fault detection, reference repair,
deliberate acknowledged-record corruption detection, record restoration,
application restart persistence, and namespace/volume cleanup. Each replicated
case also passed PostgreSQL primary switchover and database-pod replacement.
Stripe retained payment idempotency and delivered persisted webhook work after
stopping and restarting its worker, API and receipt receiver.

GitLab probes create issues and Git commits. Mattermost probes retain messages
and attachment bytes. Stripe probes retain captured payments, partial refunds,
original transaction identifiers and webhook receipts. These probes cover the
captured business transactions, not every table or user permission.

## Supporting checks

- All six registered problem IDs resolved to the intended application, tier and
  state-aware oracle in the running cluster.
- 31 focused unit tests passed, covering manifests, registry integration, oracle
  rejection paths, comparison configuration and DinD launch arguments.
- Five adapter tests passed: object serialization, metadata escaping, commit
  before acknowledgement, failed commits and cancellation before commit.
- All 83 unchanged upstream Stripe tests passed in each of two modes: original
  reference and durable adapter. Each mode passed all twelve modules with no
  skipped tests or failed-test retries in the successful run.
- The final Stripe image adds license/provenance packaging. Its executable
  sources and dependency lock match the contract-tested image byte for byte.

The imported application is SWE-Marathon's **human-written reference solution**,
not the source of an agent's passing submission. The adapter preserves its
business semantics; for example, that reference subtracts partial refunds from
PaymentIntent `amount_received`. This is a test API, not a complete Stripe or
financial-system implementation.

## Conditions and iteration history

Validation ran serially on one workstation inside a private DinD environment:
four KIND nodes, Kubernetes 1.32.11, 8 CPUs, 34 GiB outer memory, and a 24 GiB
memory-backed ext4 Docker filesystem. A 3 GiB temporary filesystem was mounted
for image exports in this running environment; the updated launcher provisions
4 GiB automatically when memory-backed Docker storage is selected. These are
validation allocations, not measured minimum requirements.

GitLab's replicated pass reused database resources from startup debugging and
created fresh business probes during the successful test. Its full readiness
probe now executes locally, consistent with GitLab's monitoring allowlist, and
its deployment progress deadline accommodates initial boot. This result is not
a cold-start benchmark. GitLab replicated and Mattermost replicated ran before
the explicit PostgreSQL shutdown budgets were reduced to 30/90 seconds; later
cases exercised those settings. Durations are retained in JSON for audit, not
used as controlled cross-tier comparisons.

The first Stripe contract run exposed missing serialization support for upstream
card-outcome objects. After that fix, both complete upstream suites passed.
The first Kubernetes admission exposed an incorrect pre-refund amount in our
business probe. The probe was corrected to the pinned reference semantics and
strengthened to verify the captured charge and original refund identity. The
failed evidence is retained alongside successful runs.

All three applications use one application instance. GitLab uses a compact
Omnibus container; Mattermost uses Team Edition; Stripe serializes state in one
PostgreSQL JSONB row. Database replicas do not establish app-tier HA. Every node
shares the same physical host, and memory-backed storage cannot demonstrate
survival of host power loss. These are foundations for richer incident families,
not implementations of the historical outages.

The final cluster snapshot contained no application namespaces or application
volumes. The private DinD container was stopped and removed, and both temporary
host inotify settings were restored to their original values.

## Evidence and reproduction

The [machine-readable report](saas-prototype-results.json) includes all case
checks, image identities, raw-evidence paths and hashes, upstream test counts,
and the standard conductor result. Full logs and verdicts are retained locally
under `results/dind/sregym-saas-prototypes/`. Initial failures use `-initial`
names. Final runtime source hashes and an archive are retained there as well;
they do not imply identical source settings across the earlier runs described
above.

The [prototype guide](saas-prototypes.md) contains preparation and validation
commands and all six registered problem IDs. The local reproduction image is
`sregym-dind:saas-prototypes`, a verified source overlay on the earlier full
DinD image. Its build log and source-hash manifest are retained with the raw
results. Building a fresh image from the checkout remains the portable path.

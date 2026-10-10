# Gitea with replicated PostgreSQL

This pilot runs the real Gitea community application, with Git repositories on
a persistent volume and a CloudNativePG-managed PostgreSQL cluster. It imports
ZOO's existing sample users, organizations, teams, and five repositories through
Gitea's APIs. The fixture, upstream commit, Apache-2.0 license, and attribution
are retained under `sregym/service/apps/fixtures/gitea-zoo/`.
Fixture accounts retain their upstream demo passwords; the bootstrap admin and
database credentials are random Kubernetes Secrets created at initial setup.

| Tier | Gitea servers | PostgreSQL members | Persistent volumes |
|---|---:|---:|---:|
| `single` | 1 | 1 | 2 |
| `replicated` | 1 | 3 | 4 |

Every volume requests 2 GiB using KIND's `standard` StorageClass. The replicated
tier requires one synchronous standby acknowledgement and also has a second
standby. Ordinary restarts reuse existing database and Git storage; they do not
restore a seed snapshot. Explicit benchmark cleanup deletes the namespace and
waits for the storage provisioner to reclaim its volumes.

The application server count stays at one in both tiers. The repository volume
uses ReadWriteOnce storage and the Deployment uses a Recreate rollout. This
pilot evaluates PostgreSQL replication and persistent application data; it does
not implement highly available shared Git storage or application-server HA.

Pinned images:

- `gitea/gitea:1.27.3`
- `ghcr.io/cloudnative-pg/postgresql:16.14-system-trixie`
- `ghcr.io/cloudnative-pg/cloudnative-pg:1.30.1`

`scripts/install_cnpg.py` verifies the published operator manifest's SHA-256
before applying it. It refuses to upgrade an existing different installation.
Install it in a **fresh dedicated DinD cluster before the first Conductor run**,
so SREGym's cluster baseline includes the operator, webhooks, and CRDs.
DinD currently uses Kubernetes 1.32.11, which CloudNativePG 1.30 lists as
[tested but outside its supported Kubernetes range](https://cloudnative-pg.io/docs/1.30/supported_releases/).
This pilot therefore needs the local lifecycle and storage checks below;
its version combination is not a production support recommendation.

## Run and validate

Build the current working tree, then run the two tiers and three Codex attempts
per tier in one dedicated environment:

```bash
python3 docker/dind/run.py build
python3 docker/dind/run.py run --name gitea-comparison --memory 32g \
  --codex-auth-file "$HOME/.codex/auth.json" -- bash -lc '
    python scripts/install_cnpg.py &&
    python scripts/evaluate_deathstarbench.py --applications gitea \
      --model gpt-6-astra --attempts 3 --profile svelte --output results/gitea
  '
```

For lifecycle validation alone, add `--validate-only`. To run the separate
storage recovery test, use a fresh environment with this payload:

```bash
python scripts/install_cnpg.py &&
python tests/integration/validate_gitea_storage.py --output results/gitea-storage.json
```

The two problem IDs are `wrong_service_selector_gitea_single` and
`wrong_service_selector_gitea_replicated`. They use the same existing selector
fault. No historical GitLab deletion incident is claimed by these tasks.

The outcome oracle requires:

- Reachable service endpoints and exactly one configured Gitea writer.
- Original bound volume identities and the configured PostgreSQL membership.
- Exactly one writable primary, a completed primary transition, and retained
  required synchronous replication settings in the replicated tier.
- A pre-fault acknowledged issue readable from every PostgreSQL member, and
  its corresponding Git file still available through the application API.
- All imported repository files unchanged, plus a fresh issue and file write.
  The new file must also exist in the native bare Git repository.

The storage test performs a controlled PostgreSQL switchover, replaces the
former primary pod, and restarts Gitea. It reruns the complete oracle after each
step and checks the original volume identities. This is not an unplanned node
failure or backup restore test. Backup selection, point-in-time recovery,
divergent-history reconciliation, full fixture integrity accounting, and a
historical recovery tail remain future incident-family work.

The pilot exposes ordinary Kubernetes access, Gitea APIs, PostgreSQL tools, and
application logs through the existing benchmark setup. It does not yet import
ZOO's other applications, browser agent, reset entrypoints, or mail/chat tools.
The background workload reads repositories, contents, and issues at 10 requests
per second; the oracle performs the checked writes.

See the [reuse assessment](application-reuse.md) for the comparison with passing
SWE-Marathon clone trials, other benchmarks, GitLab CE, and Mattermost.
The [database recovery incident](database-recovery.md) adds a logical archive,
an unusable recent backup, and an acknowledged-write recovery tail to this topology.

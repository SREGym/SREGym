# Problems from Incident Arena

SREGym includes Frappe, Saleor, Slack Spine, and ten problems from
[Incident Arena](https://github.com/abundant-ai/incident-arena).
These are ordinary SREGym problems, selected through `--problem`:

```bash
uv run main.py --problem slack_maintenance_collision --agent codex --model gpt-5.6-sol
```

The [Problem List](../Problem%20List.md) lists the problem IDs and failure mechanisms.
The selected tasks cover database limits, revoked privileges, queue failures,
connection pools, consistency modes, maintenance scheduling, and sequencer failures.
Other combinations and parameter variants from Incident Arena are not included.

## Applications

| Application | System | Workload |
| --- | --- | --- |
| Frappe | Desk API, MariaDB, Redis cache, and Redis queue workers | Desk operations, prepared reports, and background jobs |
| Saleor | GraphQL API, Celery, PostgreSQL, Valkey, and RabbitMQ | Storefront requests and checkouts |
| Slack Spine | TypeScript services, Go services, Python workers, and shared data stores | Messages, readback, sessions, and real-time delivery |

The charts are in [SREGym-applications](https://github.com/SREGym/SREGym-applications).
Each application has its own metadata, configuration, and deployment code under
`sregym/service/apps/`. Native problems and app-specific oracle checks remain under
`sregym/conductor/problems/apps/` and `sregym/conductor/oracles/apps/`.

SREGym disables the original agent harness and in-chart observability stack.
Its normal agent container, Kubernetes proxy, Prometheus, Loki, and MCP servers replace those components.
An `ops-toolbox` pod provides the original database and application repair tools.

The chart-owned load generator uses the selected task's traffic profile throughout the run.
SREGym starts its episode after application initialization completes.
For Saleor, database migration and seeding finish before the API and worker start.
The agent visibility policy hides load-generator resources.

## Evaluation

The agent receives the original incident ticket and the permitted repair scope.
Each task requires an operational repair that preserves data and required functionality.
The constraints differ by task, including limits on restarts, scaling, and configuration changes.

Diagnosis grading evaluates each independent cause separately.
A complete diagnosis must pass for every cause.
Single-cause problems use the ordinary judge path.

Mitigation grading checks the repaired state, permitted scope, and durability under the task's restart or event challenge.
It then observes client traffic for the task's recovery window.
Error rate, correct goodput, driver-specific results, and required latency measurements must meet the task's limits.
Correct goodput includes dropped arrivals in its denominator.

The oracle selects a fixed arrival window and waits for its requests to finish before it reads their results.
A replaced workload episode or missing required latency samples cannot establish recovery.
The healthy latency baseline follows the same measurement rules.

The outcome limits use Incident Arena's 20% tolerance.
For latency, each limit is at least twice the healthy latency measured on the same cluster before injection.
Tasks that deploy a faulty image from startup retain their original latency limits because no healthy baseline exists.
Error-rate and goodput limits do not change with the measured baseline.

## Requirements

The pinned images provide Linux/AMD64 and Linux/ARM64 builds.
Native ARM execution testing is still pending.
The cluster needs capacity for the application and SREGym's observability stack.
Slack Spine contains about 40 pods.
Frappe's first site creation and Saleor's database initialization can take several minutes.

The charts use shared ReadWriteOnce volumes.
The supported local provisioners, `openebs-hostpath` and KIND's `local-path`, keep consumers on the volume's node.
SREGym supplies the initial application capacity through each app's `values.yaml`.
The task's oracle captures that baseline before fault injection.

## Provenance

The task contracts come from Incident Arena commit
`fba011e451653c4058c2dc80a97c5d260c036872`.
They remain unchanged under `sregym/conductor/problems/contracts/incident_arena/tasks/`.
The [contract README](../sregym/conductor/problems/contracts/incident_arena/tasks/README.md)
describes the source files and their uses.
Application READMEs retain chart provenance and license notices.

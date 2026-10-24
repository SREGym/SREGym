# SREGym-Lite on Frappe, Saleor and Slack Spine (`sregym-lite-ia`)

This suite runs the current SREGym-Lite faults on Frappe/ERPNext, Saleor and
Slack Spine instead of Hotel Reservation, Social Network and Astronomy Shop.

Each port adapts an existing fault to a real component of the new application.
Its mitigation oracle checks both the repair and application recovery.
`LoadgenHealthOracle` checks a fresh, closed 120-second traffic window after a
30-second settling period. It checks correct responses, dropped requests,
traffic coverage, and latency against the healthy baseline.
The shared-memory worker fault instead checks the web API and a fresh background job.

Problems and oracles live in their existing `apps/<app>/` packages, grouped by
fault family. `problems/environment_ports.py` records the source-to-port mapping.
Named suites remain in `problem_sets.py`.

```bash
uv run main.py --suite sregym-lite-ia --agent codex --model gpt-6-sol --judge-backend codex
```

| Lite problem | Port | Target |
|---|---|---|
| cronjob_sidecar_blocks_completion_hotel_reservation | cronjob_sidecar_blocks_completion_frappe | CronJob in the Frappe namespace |
| mutating_webhook_resource_limits_social_network | mutating_webhook_resource_limits_slack_spine | `svc-message` |
| internal_traffic_policy_local_astronomy_shop | internal_traffic_policy_local_slack_spine | `svc-notification` |
| unschedulable_incorrect_port_assignment | unschedulable_incorrect_port_assignment_frappe | `erp-nginx` `BACKEND` |

Notes:
- Frappe deploys with best-effort hostname spread (`ScheduleAnyway`): its pods share a node-local
  RWO volume on kind, and the chart's `DoNotSchedule` spread makes every rolling update unschedulable.
- Several ports roll the client after injection: keep-alive connections otherwise survive Service,
  DNS and NetworkPolicy changes and hide the fault from users.

## Running campaigns in parallel

`docker/dind/campaign.py` runs one problem (N attempts) per DinD container. Every container has a
private Docker daemon and kind cluster, so pass `--registry-mirrors` (sets `SREGYM_REGISTRY_MIRRORS`,
see the DinD README) to avoid Docker Hub's unauthenticated pull limit.

Each launch gets a unique container name and result directory. Retries cannot
reuse stale CSVs. A problem is complete only when the process exits successfully
and records every requested attempt as complete. Incomplete campaigns exit nonzero.
Authentication copies stay in private temporary storage outside the result directory.

Use `--max-containers-file` to limit host-wide concurrency, `--suffix .topup1`
to separate campaign outputs, and `--env KEY=VALUE` for additional settings.

## Healthy deployment defaults

The suite removes these unrelated deployment problems before fault injection:

| App | Was | Now |
|---|---|---|
| Slack Spine | svc-message held its DB connection for 150 ms per request (peers 5–12 ms) | `db.hold_ms` 10 (`sregym/service/apps/slack_spine/values.yaml`) |
| Frappe | scheduler Deployment at 0 replicas and the site's scheduler disabled | scheduler runs and is enabled on the site |
| Saleor | API and worker started during the init Job's migrations and logged schema errors that stay in Loki | both start once the Jobs finish |

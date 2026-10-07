# SREGym-Lite on Frappe, Saleor and Slack Spine (`sregym-lite-ia`)

Environment scaling for SREGym 2.0: the 21 SREGym-Lite faults re-targeted from Hotel Reservation,
Social Network and Astronomy Shop to three much larger applications: Frappe/ERPNext, Saleor and
Slack Spine.

Each port keeps the original fault's causal mechanism and state-based mitigation oracle, aimed at a
real component of the new app, and adds one requirement: after the agent finishes, the chart's own
load generator must serve traffic about as well as before the fault (`LoadgenHealthOracle`, a fresh
60 s window measured after a 30 s settle). Ports live in `sregym/conductor/problems/lite_ia/`;
`LITE_IA_PORTS` maps every Lite id to its port.

```bash
uv run main.py --suite sregym-lite-ia --agent codex --model gpt-6-sol --judge-backend codex
```

| Lite problem | Port | Target |
|---|---|---|
| cronjob_sidecar_blocks_completion_hotel_reservation | cronjob_sidecar_blocks_completion_frappe | CronJob in the Frappe namespace |
| edge_request_filter_cpu_saturation | edge_request_filter_cpu_saturation_frappe | `erp-nginx` edge (Service `erp`) |
| network_policy_block | network_policy_block_slack_spine | `svc-auth` |
| env_variable_shadowing_astronomy_shop | env_variable_shadowing_saleor | `saleor-api` `DATABASE_URL` |
| mutating_webhook_resource_limits_social_network | mutating_webhook_resource_limits_slack_spine | `svc-message` |
| finalizer_deadlock_controller_hotel_reservation | finalizer_deadlock_controller_frappe | cleanup controller RBAC |
| kafka_poison_pill_hol_block | kafka_poison_pill_hol_block_slack_spine | Redpanda `jobs.index` lane (`worker-index`) |
| internal_traffic_policy_local_astronomy_shop | internal_traffic_policy_local_slack_spine | `svc-notification` |
| service_dns_resolution_failure_social_network | service_dns_resolution_failure_slack_spine | `svc-thread` |
| service_wrong_pod_selection_hotel_reservation | service_wrong_pod_selection_frappe | `svc-frappe-web` selects `erp-worker-l` |
| namespace_memory_limit | namespace_memory_limit_saleor | quota blocks `postgres-0` (metrics sidecar) |
| valkey_auth_disruption | redis_auth_disruption_slack_spine | shared `redis` |
| secret_rotation_stale_env_credentials_astronomy_shop | secret_rotation_stale_env_credentials_saleor | `saleor-api` / `saleor_app` role |
| unschedulable_incorrect_port_assignment | unschedulable_incorrect_port_assignment_frappe | `erp-nginx` `BACKEND` |
| readiness_probe_misconfiguration_social_network | readiness_probe_misconfiguration_slack_spine | `svc-message` |
| duplicate_pvc_mounts_social_network | duplicate_pvc_mounts_slack_spine | `svc-search` |
| admission_webhook_outage_hotel_reservation | admission_webhook_outage_saleor | `saleor-api` |
| wrong_dns_policy_astronomy_shop | wrong_dns_policy_saleor | `saleor-api` |
| wrong_service_selector_social_network | wrong_service_selector_slack_spine | `svc-channel` |
| rolling_update_misconfigured_social_network | rolling_update_misconfigured_frappe | `erp-worker-l` |
| search_rate_retry_collapse_hotel_reservation | search_rate_retry_collapse_slack_spine | `svc-channel` → `svc-workspace` mesh retries |

Notes:
- Frappe deploys with best-effort hostname spread (`ScheduleAnyway`): its pods share a node-local
  RWO volume on kind, and the chart's `DoNotSchedule` spread makes every rolling update unschedulable.
- Several ports roll the client after injection: keep-alive connections otherwise survive Service,
  DNS and NetworkPolicy changes and hide the fault from users.

## Running campaigns in parallel

`docker/dind/campaign.py` runs one problem (N attempts) per DinD container. Every container has a
private Docker daemon and kind cluster, so pass `--registry-mirrors` (sets `SREGYM_REGISTRY_MIRRORS`,
see the DinD README) to avoid Docker Hub's unauthenticated pull limit.

Campaigns that run at the same time need distinct `--name-prefix` values: containers are named after
the problem, and a second campaign would otherwise remove the first one's running container. Use
`--max-containers-file` to cap DinD containers host-wide across campaigns (the limit is re-read, so it
can be changed while campaigns run), `--suffix .topup1` to add attempts in a separate results
directory, and `--env KEY=VALUE` to pass settings such as `SREGYM_CLEANUP_DRAIN_TIMEOUT_S` into
each run.

## Natural-noise ablation (experiment only)

`SREGYM_ABLATE_NATURAL_NOISE=1` removes three harmless properties of the larger apps that agents
often mistake for the fault:

| App | Decoy | Change |
|---|---|---|
| Slack Spine | svc-message holds DB connections for 150 ms (peers use 5–12 ms) | `db.hold_ms` set to 10 |
| Frappe | the scheduler ships at 0 replicas | scheduler scaled to 1 |
| Saleor | API and worker log schema errors while the init Job migrates | both held at 0 replicas until the Jobs finish, then released with `helm upgrade` |

Default deployments are unchanged. Restarting Saleor's API after migrations is not enough: Loki
keeps the old errors and agents query it.

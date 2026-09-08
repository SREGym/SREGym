# Apple silicon SREGym-Lite validation

Local validation on September 4, 2026 (America/Chicago), addressing the Lite
portion of [issue #1001](https://github.com/SREGym/SREGym/issues/1001).

## Environment

| Component | Tested configuration |
|---|---|
| Host | Apple silicon, macOS 26.5, 24 GiB RAM |
| Linux VM | OrbStack 2.2.3, 15 CPUs, 16 GiB configured memory |
| Cluster | KIND, one control-plane and three workers, all ARM64 |
| Kubernetes / networking | v1.32.1 / Calico v3.27.0 |
| kubectl | v1.33.9, `darwin/arm64` on the host and `linux/arm64` in the agent image |
| Source bases | SREGym `5bf33ec5`; applications submodule `2b2f9c6`, plus local compatibility changes |

These are functional compatibility checks, not performance or leaderboard
measurements. Early runs were interrupted by Mac sleep; subsequent runs used
`caffeinate`. The Linux VM's memory allocation was raised from 12 to 16 GiB for
the full-profile sweep.

## Compatibility changes

- Auto-detect the KIND node architecture and avoid GNU-only shell options on macOS.
- Build and load missing native Hotel Reservation, Social Network, wrk2, and
  Locust-exporter images. Build recipes live under `docker/` or the applications
  submodule; `kind/` contains the orchestration scripts.
- Repair the legacy Social Network source builds and shallow-clone its startup
  assets to avoid downloading the entire upstream Git history for each pod.
- Install native kubectl in the agent image, matching the host client by default.
- Register Astronomy Shop's Helm dependencies on fresh installations, give its
  two ARM Go services adequate memory, and cap the feature-flag UI's descriptor
  limit without increasing its memory budget. Svelte omits that optional UI.
- Report explicit container-platform failures from pod status or failed-container
  logs promptly, and avoid repeating deployment attempts for that permanent error.
- Add repeatable lifecycle, cleanup, native agent-connectivity, and CLI-bootstrap
  checks without requiring model credentials.

## Live fault lifecycles

The runner uses the real Conductor, application deployments, normal workloads,
fault injectors, mitigation oracles, and recovery functions. A pass requires the
oracle to report failure after injection, success after recovery, and successful
application cleanup. Some oracles additionally replace pods or check continuing
data-plane progress. No LLM performs the recovery in these checks.

Profile: **full**, including the shared Prometheus, Jaeger, OTel, Loki/Promtail,
MCP, and OpenEBS infrastructure. Astronomy Shop also retains its bundled
OpenSearch, Grafana, and feature-flag UI.

| Problem | Result |
|---|---|
| `cronjob_sidecar_blocks_completion_hotel_reservation` | PASS |
| `edge_request_filter_cpu_saturation` | PASS |
| `network_policy_block` | PASS |
| `env_variable_shadowing_astronomy_shop` | PASS |
| `mutating_webhook_resource_limits_social_network` | PASS |
| `finalizer_deadlock_controller_hotel_reservation` | PASS |
| `kafka_poison_pill_hol_block` | PASS |
| `internal_traffic_policy_local_astronomy_shop` | PASS |
| `service_dns_resolution_failure_social_network` | PASS |
| `service_wrong_pod_selection_hotel_reservation` | PASS |
| `namespace_memory_limit` | PASS |
| `valkey_auth_disruption` | PASS |
| `secret_rotation_stale_env_credentials_astronomy_shop` | PASS |
| `unschedulable_incorrect_port_assignment` | PASS |
| `readiness_probe_misconfiguration_social_network` | PASS |
| `duplicate_pvc_mounts_social_network` | PASS |
| `admission_webhook_outage_hotel_reservation` | PASS |
| `wrong_dns_policy_astronomy_shop` | PASS |
| `wrong_service_selector_social_network` | PASS |
| `rolling_update_misconfigured_social_network` | PASS |
| `search_rate_retry_collapse_hotel_reservation` | PASS |

Raw local logs and per-problem Markdown/JSON results are under
`.runtime/mac-validation/full/`; the aggregate is `suite.json`. Runtime
artifacts are intentionally not tracked in Git.

After the last compatibility fixes, three independent **full-profile** reruns
passed from fresh application deployments: `edge_request_filter_cpu_saturation`,
`env_variable_shadowing_astronomy_shop`, and
`mutating_webhook_resource_limits_social_network`. All required fault, recovery,
oracle, and cleanup stages passed. Their results are in
`.runtime/mac-validation/full-confirmation/`.

Three final-code **svelte** lifecycles also passed: `network_policy_block`,
`env_variable_shadowing_astronomy_shop`, and
`wrong_service_selector_social_network`. These cover all three Lite applications
and are recorded in `.runtime/mac-validation/svelte-confirmation/`. They reused
the existing full-profile shared monitoring stack, so they verify application
overlays and fault behavior, not a fresh svelte infrastructure installation or
its total memory consumption.

## Additional checks

- Broad non-live test suite: **741 passed, 1 skipped**. The command below excludes
  separate live-cluster, CloudLab, file-editing, and model-judge suites. The skip
  is a trace-store test requiring untracked real campaign trajectory fixtures.
- Agent-container connectivity: **2 passed**, covering open and filtered network
  modes. Each queried Kubernetes and all four MCP endpoints, including real
  Prometheus metrics, Loki labels, and Jaeger services.
- Native CLI bootstrap: **5 passed** in disposable, filtered-network containers
  without host credentials or model calls: Claude Code 2.1.261, Codex 0.153.4,
  Gemini 0.58.0, OpenCode 1.18.29, and Copilot 1.0.83.
- Complete native image build/load script rerun successfully with cached builds.
  Runtime-image audits found only ARM64 images: 75 node/image combinations in
  the Astronomy/monitoring snapshot, and 66 in a subsequent snapshot.
- Real incompatible-executable test: an ARM-tagged fixture containing an x86
  executable failed under KIND; the log-based platform diagnostic reported it
  in **2.02 seconds**, including its pod, container, image, and node. The fixture
  pod and tagged image were removed afterward.
- Feature-flag UI: with its descriptor limit capped, a ten-minute check served
  the UI with **zero restarts**, using about 173 MiB under the original 250 MiB
  limit. Increasing memory alone to 2 GiB had not fixed the startup failure.
- Helm rendering verified both full and svelte overlays, including the UI
  override and ARM Go-service memory settings. Earlier svelte application/fault
  smoke checks covered all three Lite applications and native wrk2 traffic.
- Python lint/format checks, Bash syntax checks, and Git whitespace checks passed.

## Reproduce

Create the disposable KIND cluster and native images using the
[Lite setup instructions](SREGym-Lite.md). Then, on macOS:

```bash
caffeinate -i uv run python tests/integration/validate_lite.py \
  --profile full --output-dir .runtime/lite-validation/full

uv run pytest tests/integration/test_agent_connectivity.py \
  tests/integration/test_agent_cli_bootstrap.py -m integration -v

uv run pytest tests --ignore=tests/integration \
  --ignore=tests/e2e-testing-scripts --ignore=tests/file_editing \
  --ignore=tests/kubectl_tool_tests \
  --ignore=tests/llm_as_a_judge/test_stratus_rejudge.py -q
```

Use `--resume` only after checking a failed run's cleanup, and only with the same
cluster/profile. Use a new output directory for independent confirmation runs.

## Scope and limitations

At completion, all four nodes and every remaining infrastructure pod were Ready.
All test application namespaces, fault webhook configurations, and test volumes
were gone; only the shared monitoring volumes remained. No agent/proxy containers
or validation proxy listeners remained. The native images and healthy KIND
cluster were retained for subsequent use, with OrbStack still configured for
16 GiB of VM memory.

This validates the local Apple silicon/OrbStack path, not Docker Desktop, Intel
Mac hardware, every SREGym application, or a complete LLM-driven agent campaign.
TrainTicket and hardware/Khaos faults remain outside Lite's scope. Native images
were built and loaded locally, not published to registries. Sharing the changes
requires committing the application-submodule fixes as well as the main-repo
changes and updating the submodule reference appropriately.

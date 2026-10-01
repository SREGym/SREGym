# Incident Arena charts (vendored)

These Helm charts come from [abundant-ai/incident-arena](https://github.com/abundant-ai/incident-arena)
at commit `fba011e451653c4058c2dc80a97c5d260c036872`. Their source of truth is
[abundant-ai/sre-world](https://github.com/abundant-ai/sre-world) (`substrates/<name>/chart`).
Both are Apache-2.0; see `../LICENSE-incident-arena`. The vendored upstream
`frappe/helm` ERPNext chart keeps its own notice in `frappe/charts/erpnext/LICENSE-UPSTREAM.md`.

| Chart | Copied from task | Notes |
|---|---|---|
| `frappe/` | `000--frappe--07-deletes-and-jobs-fail-194d9279/environment/chart` | Every Frappe task ships a byte-identical chart. |
| `saleor/` | `006--saleor-spine--10-T1-statement-timeout-canary-c7dcd6d4/environment/chart` | |
| `slack-spine/` | `018--slack-spine--09-I1-seq-lock-leak-0b7c2973/environment/chart` | Plus `files/sequencer_config_broker.py` from task 007 and `templates/tier06.yaml` from task 008, so one chart covers all 13 Slack tasks. |

Each task's answer key (`ground-truth.yaml`), rendered `config-before.json` and
Oddish egress host list were removed from the chart directories. The task
contracts SREGym reads live in `sregym/conductor/problems/incident_arena/tasks/`
and are never rendered into a cluster.

## SREGym edits

All edits are inert unless `sregym.enabled` is true, so the charts still render
the original Incident Arena tasks when it is false. SREGym deploys them with
`../values/<chart>.yaml`, which also switches off the Harbor harness through the
charts' own values: the agent foothold (`main`) with its egress proxy, DNS
filter, agent freezer and grader broker, and the in-chart Prometheus, Loki,
promtail and obs-mcp. SREGym's cluster-wide observability stack replaces these.

* **All charts**
  * `values.yaml` gains an `sregym:` block with defaults that do nothing.
  * `templates/loadgen.yaml` adds `sregym.loadgenLabels` to every load generator
    resource. SREGym sets `app: load-generator`, which its agent visibility
    policy hides.
  * `templates/loadgen.yaml` renders `sregym.neutralAnswerKey` as the load
    generator's answer key. The load generator refuses to start without one, and
    SREGym grades with its own oracles.
  * New `templates/sregym-toolbox.yaml`: an `ops-toolbox` Deployment running the
    task's `main` image. It provides the operator CLIs (`psql`/`mysql` with the
    privileged DSN, `restart-svc.sh`, `reconfigure-infra.sh`, ...), with
    `submit_incident_report` and `declare_repair_complete` replaced by stubs.
* **frappe**: `templates/obs.yaml` keeps the MariaDB and Redis exporters but drops
  the in-chart Prometheus, Loki, promtail and obs-mcp.
* **saleor**: `templates/loadgen.yaml` runs the image's `loadgen_sidecar.py`
  directly instead of the grading shim, which only adds Incident Arena evidence
  capture.
* **slack-spine**: `templates/loadgen.yaml` skips the verifier-only baseline
  capture init container and its ConfigMap.

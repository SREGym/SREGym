# substrates/frappe/chart — Frappe substrate Helm chart

Wraps the vendored upstream `frappe/helm` v8.0.65 (at `charts/erpnext/`) with
the SRE-World-substrate additions: operator-shell foothold (`main`),
out-of-band loadgen with protected evidence collection, observability MCP + Prometheus/Loki
stack, and per-workload `frappe-admin` sidecars. D16 peer to
`substrates/slack-spine/chart` (Slack-spine).

## Layout

```
chart/
├── Chart.yaml            # wrapper; depends on charts/erpnext (v8.0.65 pin)
├── values.yaml           # upstream overrides + SRE-World addition knobs
├── charts/
│   └── erpnext/          # VENDORED upstream frappe/helm — DO NOT hand-edit
│       ├── Chart.yaml    #   pinned to v8.0.65 (matches wrapper dependency)
│       ├── templates/    #   upstream workload templates
│       ├── values.yaml   #   upstream defaults (superseded by ../../values.yaml.erpnext)
│       └── charts/       #   bitnami mariadb/redis .tgz subcharts (cached)
└── templates/            # SRE-World additions — main, loadgen, obs, sidecar
```

## Vendoring policy

The upstream chart is a **direct copy**, not a git submodule (D14 task-directory
rule + D16 vendoring discipline). Refresh procedure when bumping upstream:

```bash
git clone --depth 1 --branch v<TAG> https://github.com/frappe/helm.git /tmp/frappe-helm-bump
rm -rf substrates/frappe/chart/charts/erpnext
cp -R /tmp/frappe-helm-bump/erpnext substrates/frappe/chart/charts/erpnext
# Update Chart.yaml dependency version + our values.yaml if schema changed.
# Run: helm template t substrates/frappe/chart | head to sanity-check.
# Run: ./validate.sh frappe-smoke (Phase 5+) to confirm no schema drift.
```

Upstream carries an MIT-style license at `charts/erpnext/LICENSE-UPSTREAM.md`.

## Render sanity-check

```bash
helm template t substrates/frappe/chart | grep -c '^kind:'   # ~34 manifests at Phase 1
```

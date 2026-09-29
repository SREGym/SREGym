# Network access

Filtered access is the default. It allows the selected model provider and internal SREGym services, but blocks other internet destinations.

## Endpoint configuration

For a custom Stratus, Codex, or local OpenCode endpoint, set `AGENT_API_BASE`.
See [local models](../README.md#local-llms) for examples and the host-interface requirement.

Use `--allow-agent-endpoint` for an extra destination:

```bash
uv run main.py --agent codex --model gpt-5.6-sol \
  --allow-agent-endpoint https://telemetry.example.com/v1
```

The URL permits its host, port, path, and child paths. A URL without a path permits all paths on that host and port.
The option accepts multiple occurrences. It has no effect with `--internet-access open`.

Use `--internet-access open` for unrestricted internet access.

## Cluster maintenance

Filtered mode requires Calico 3.29 or later with policy tiers. The [kind setup script](../kind/setup_kind_cluster.sh) installs Calico 3.29.3.
Older kind clusters will need a Calico upgrade before filtered runs can start.

### Upgrade an existing kind cluster

These commands apply to the repository's kind setup, not custom Calico installations.

1. Stop all benchmark runs.
2. Verify that `kubectl config current-context` selects the intended kind cluster.
3. Run the upgrade commands:

```bash
kubectl apply --server-side --force-conflicts -f https://raw.githubusercontent.com/projectcalico/calico/v3.29.3/manifests/calico.yaml
kubectl rollout status daemonset/calico-node -n kube-system --timeout=240s
kubectl rollout status deployment/calico-kube-controllers -n kube-system --timeout=240s
kubectl get tiers.crd.projectcalico.org
```

4. Verify that the output includes the `adminnetworkpolicy` tier.
5. Remove the old baseline as described in the next section.

### Remove the old baseline after an upgrade

After an upgrade, remove `~/cache_dir/cluster_baseline_state.json` from the machine that runs SREGym.
Do this only with the cluster healthy, all runs stopped, no active fault, and benchmark application namespaces removed.
SREGym captures a new baseline on the next run.

```bash
rm ~/cache_dir/cluster_baseline_state.json
```

# Local compatibility checks

These checks use the normal application deployment, workload, injector, recovery,
and mitigation oracle. They make no model API calls.

## Fault lifecycles

Use a disposable local KIND cluster. These checks delete application namespaces
and exercise cluster-scoped faults. Do not use a cluster that contains other work.

```bash
uv run python tests/integration/validate_lite.py \
  --profile full --output-dir .runtime/lite-validation/full
```

The runner checks that the oracle fails after injection and passes after recovery.
It also checks cleanup, writes reports, and stops on the first failure. Inspect
the report and cluster state before continuing with `--resume`. Resume requires
the same cluster and profile.

Use `--problems PROBLEM_ID` to select faults. Use a separate output directory for
`--profile svelte`. On macOS, prefix the command with `caffeinate -i` to prevent
idle sleep. Closing the lid can still interrupt the run.

## Agent connections and CLI startup

After the shared monitoring stack is deployed, run:

```bash
uv run pytest tests/integration/test_agent_connectivity.py \
  tests/integration/test_agent_cli_bootstrap.py -m integration -v
```

The checks use disposable agent containers. They query Kubernetes and the MCP
services in open and filtered modes, then check CLI installers without model
credentials. CLI startup is not a complete agent-run test.

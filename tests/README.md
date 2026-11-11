# Running tests without cluster access

Unit tests cannot reach a Kubernetes cluster. `tests/conftest.py` blocks
Kubernetes client calls, kubeconfig loading, and `kubectl`/`helm` commands, and
fails the test even if the code catches the error. Mock cluster dependencies in
unit tests.

## Integration tests

Tests that need a real cluster must be marked `@pytest.mark.integration` and run
with `--allow-kubernetes`:

```sh
uv run pytest tests/integration -m integration --allow-kubernetes
```

Without the flag, integration tests are skipped. Unmarked tests stay blocked even
with the flag. Create live clients inside fixtures or test bodies, not at module
level, because test collection is always blocked.

## Local test servers and kubeconfigs

- `allow_kubernetes_test_server(server)` allows the Kubernetes client to talk to
  a test's own `127.0.0.1` HTTP server.
- `allow_kubernetes_test_config(*paths)` allows loading kubeconfig files the test
  wrote under its `tmp_path`.

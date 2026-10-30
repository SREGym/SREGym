# Running tests without cluster access

`uv run pytest tests` blocks Kubernetes SDK API calls, kubeconfig/in-cluster
configuration loading, and execution of `kubectl` or `helm` commands. Tool names
used as ordinary data (for example, `ls sregym/helm`) are not blocked.
Shell syntax is parsed with `bashlex`, so commands inside conditionals, loops,
backticks, and `$(...)` are checked too. `xargs` and shell `-c` wrappers are
inspected at their command positions. Recognized cluster commands fail loudly
even if the caller catches the exception. For unsupported shell syntax, cluster
tool or Kubernetes-environment references are rejected conservatively; unrelated
local scripts remain allowed. Quoted literal command text in supported syntax
remains ordinary data.
Direct `helm template` calls with basic local rendering options (such as
`--namespace`, `--values`, and `--set`) are allowed. Shell-wrapped rendering,
server validation, post-renderers, and unknown options remain blocked.
Direct `helm dependency list/update CHART` calls are also allowed; these manage
chart files and may access chart repositories, but do not contact Kubernetes.
The guard is installed before test collection and stays active through fixture
teardown. Mock cluster dependencies in unit tests. If application code catches
the guard's exception, the recorded attempt still makes the test fail at teardown.

Before collection, the guard points `KUBECONFIG` at a private, nonexistent file
and removes Kubernetes service-discovery variables and `KUBERNETES_MASTER`.
Process launches through `Popen` (including `subprocess.run`), `os.system`, and
`posix_spawn`/`posix_spawnp` also sanitize their child environment, even if the
caller supplies a fresh `env` mapping. The caller's mapping is not modified.
The legacy `os.spawnv*` and `os.spawnl*` families receive the same command checks
and environment isolation, including their explicit-environment variants.
Explicit `--kubeconfig` arguments are rejected on these guarded launch paths.

Integration tests are skipped by default. To intentionally use a test cluster:

```sh
uv run pytest tests/integration -m integration --allow-kubernetes
```

Both the `integration` marker and `--allow-kubernetes` are required to disable the
guard for a test, including its fixture setup and teardown. Unmarked tests remain
guarded even with the flag. Collection always remains guarded: initialize live
clients in fixtures or test bodies, not at module scope.
Opted-in integration tests receive the original Kubernetes environment during
setup, execution, and teardown. Isolation is reinstated afterward, and pytest
cleanup restores the process's original environment. The CI smoke workflow
passes `--allow-kubernetes` explicitly.
Threads started with `threading.Thread` inherit their originating test's access
context, which is revoked when that test ends. An old unit-test thread cannot
borrow a later integration test's permission; an integration thread that outlives
its test becomes guarded again. Reused worker pools retain their originating
thread context, so create cluster-using pools inside the opted-in test.

Tests exercising the SDK against their own local HTTP server can call the
`allow_kubernetes_test_server(server)` fixture with their bound `HTTPServer`.
Only that server's exact loopback address and port are allowed, while its socket
remains open. Other loopback endpoints (including a local Kind cluster) remain
blocked. A bound socket is also accepted for connection-refusal tests. SDK
redirect destinations are checked against the same registered sockets. SDK
WebSocket connections (exec and port-forwarding) remain blocked even when the
HTTP transport is mocked. SDK transport tests may replace
`pool_manager.request` with an inert `Mock` (without
`side_effect` or `wraps`); that cannot send a network request.

Configuration parser tests can register their generated files with
`allow_kubernetes_test_config(*paths)`. Paths must be inside that test's
`tmp_path`, and loaders must receive those paths explicitly. Default kubeconfig
loading and network access using the resulting configuration remain blocked.

This is protection against accidental unmocked calls, not a process sandbox.
Arbitrary code inside a child can still load a hard-coded credential file,
reconstruct flags after launch, change its environment, or use custom HTTP
clients. Credentials inside containers or remote hosts are not isolated by this
guard. The process environment is still shared; the SDK and launch wrappers
enforce the calling thread's access context, but unwrapped access paths do not.
Standalone
validation scripts run directly with Python are outside pytest and this guard.

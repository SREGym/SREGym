# Copilot instructions for SREGym

## Build, test, and lint commands

- Install dependencies: `uv sync` (Python >= 3.12; `uv.lock` is committed).
- Install git hooks: `uv run prek install`.
- Run the benchmark: `python main.py --agent stratus --model gpt-5`.
- Run one benchmark problem: `python main.py --problem misconfig_app_hotel_res --agent stratus --model gpt-5`.
- Rebuild the isolated agent image after changing agent/container dependencies: `python main.py --agent stratus --model gpt-5 --force-build` or `bash docker/agents/build.sh`.
- Generate and run a problem from an issue URL: `python main.py --create <github-issue-url> --agent stratus --model gpt-5`.
- Useful `main.py` flags: `--n-attempts N` (repeat each problem), `--resume <results.csv>` (skip problems already in the CSV), `--judge-model`, `--agent-timeout SECONDS` (per-problem agent budget after deployment, default 1800), `--noise` (Chaos Mesh noise injection), `--use-external-harness` (inject fault and exit, no agent), `--deploy-observability` (Prometheus/Jaeger/OTel/Loki), `--skip-openebs`.
- Interactive developer CLI (talks to the in-process Conductor for testing problems, not for benchmarking): `python cli.py`, then `start <problem_id>`.
- Run only a problem's mitigation oracle against an already-deployed, already-injected cluster: `python run-oracle.py --problem <problem_id>`.
- Run problems distributed across nodes (e.g. CloudLab/self-hosted): `uv run python -m distributed --hosts distributed_hosts.yaml --agent stratus --model gpt-5`.
- Run all pytest tests: `uv run pytest`. Some tests require Docker, kind, kubectl, Helm, and a usable Kubernetes context.
- Run one pytest test: `uv run pytest path/to/test_file.py::ClassName::test_name -v`.
- Run one parametrized file-editing test case: `uv run pytest tests/file_editing/test_file_editing_tool.py::TestOpenFile::test_open_file_success -v -k test_open_file_1`.
- Run kubectl tool tests: `uv run pytest tests/kubectl_tool_tests/kubectl_tool_set_test.py::TestKubectlTools::test_kubectl_tools_success -v -s` (requires Kubernetes).
- Run the integration smoke test: `uv run pytest tests/integration/smoke_test.py::test_smoke_misconfig_app_hotel_res -v -s -m integration`.
- Run lint/format hooks: `uv run prek run --all-files`. Individual hooks include `uv run prek run ruff-check --all-files` and `uv run prek run ruff-format --all-files`.

## High-level architecture

- `main.py` is the benchmark driver. It ensures a kind cluster exists, sets runtime environment variables (`AGENT_MODEL_ID`, `JUDGE_MODEL_ID`, `API_HOSTNAME`, `MCP_SERVER_URL`), starts the Conductor HTTP API, launches agents from `agents.yaml`, writes results under `results/<timestamp>/...`, and supports single-problem, retry, resume, noise, external-harness, and issue-generated runs.
- `sregym/conductor/conductor.py` owns the problem lifecycle: load a problem from `ProblemRegistry`, read `sregym/conductor/tasklist.yml` if present, clean leftovers, deploy cluster support components, deploy the app, start workload, inject the fault, advance through `diagnosis` and `mitigation`, evaluate oracles, recover the fault, undeploy the app, and reconcile cluster state.
- Problems live in `sregym/conductor/problems/` and extend `Problem`. A problem constructs an app from `sregym/service/apps/`, sets a structured `root_cause`, attaches diagnosis/mitigation oracles, and implements `inject_fault()` / `recover_fault()` using fault injectors from `sregym/generators/fault/`.
- Application wrappers in `sregym/service/apps/` load metadata from `sregym/service/metadata/*.json` and deploy Helm charts or Kubernetes manifests from `SREGym-applications/`. Workloads are started through workload managers in `sregym/generators/workload/`.
- Agents are registered in `agents.yaml`. `AgentLauncher` and `ContainerRunner` run most agents inside the `sregym-agent-base:latest` Docker image, forward model/provider environment variables, mount logs at `/logs`, mount selected benchmark app assets read-only, and mount code-level bug source trees at `/opt/source` when a problem provides one.
- The in-cluster MCP server is under `mcp_server/` and is deployed by `sregym/service/mcp_server.py` using `kubectl apply -k mcp_server/k8s`. It is port-forwarded to local port `9954` and exposes SSE routes for kubectl, Jaeger, Loki, Prometheus, submit, and rebuild tools. Stratus maps configured tool names to concrete tools in `clients/stratus/stratus_utils/str_to_tool.py`.
- `sregym/service/k8s_proxy.py` filters Kubernetes API views for agents, hiding chaos/load-generator resources. The driver starts this proxy before launching containerized agents and passes the generated kubeconfig into agent containers.
- Auto problem construction: `--create <issue-url>` (programmatically `ProblemGenerator.generate(url)`) parses a GitHub/Jira database bug report, resolves the buggy version and git ref, extracts a reproducer, and renders `sregym/conductor/problems/auto_<db>_<number>.py` as a subclass of `GenericCustomBuildProblem` backed by `GenericDBApplication`, keyed off `db_name` in `DB_REGISTRY`. Generated files are auto-discovered by `ProblemRegistry`. For the full construction/runtime pipeline (image build, buggy-image swap, reproducer, oracles) and how to add a database, see the skill `.github/skills/sregym-db-bug-reproduction/`.
- Each agent in `agents.yaml` maps a name to a `kickoff_command` driver module under `clients/<agent>/`. CLI-based agents (`codex`, `claudecode`, `gemini`, `opencode`) are installed into the base image via `docker/agents/install-scripts/*.sh` referenced by `install_script`. Setting `container_isolation: false` (e.g. `resolve`, `demo`) runs the agent on the host instead of inside the isolated image.

## Key conventions

- Target Python is 3.12. Formatting/lint config uses 120-character lines, Ruff format with double quotes, and Ruff import sorting with first-party packages `sregym`, `clients`, `provisioner`, and `scripts`.
- Treat `SREGym-applications/` as bundled benchmark application artifacts/submodules. The root Ruff config excludes it; core code searches and linting should avoid its vendored, generated, `node_modules`, and third-party contents unless the task specifically targets an application artifact.
- Use `sregym.paths` for repository-relative paths such as `TARGET_MICROSERVICES`, metadata files, fault scripts, and MCP Kubernetes manifests instead of hard-coded relative strings.
- To add a problem, create the `Problem` subclass, attach oracles, decorate both `inject_fault()` and `recover_fault()` with `@mark_fault_injected`, register the problem ID in `sregym/conductor/problems/registry.py`, and add it to `sregym/conductor/tasklist.yml` when the run should be limited or stage order customized.
- Use `Problem.build_structured_root_cause(component=..., namespace=..., description=...)` for judge-facing root-cause text; this produces the `[fault_spec] component=...; namespace=... || ...` format expected by diagnosis evaluation.
- If `sregym/conductor/tasklist.yml` exists, only listed problem IDs are run by default and each problem's stage list must be an ordered subset of `diagnosis`, `mitigation`. If it does not exist, the registry defaults to all problems and the Conductor defaults each problem to diagnosis plus mitigation.
- Application metadata keys are capitalized (`Name`, `Namespace`, `Helm Config`, `K8S Deploy Path`). `Application.load_app_json()` validates local Helm chart and manifest paths relative to `SREGym-applications/`.
- Cluster cleanup is part of the benchmark contract: app wrappers should clean namespaces, workload jobs, and app-specific persistent resources; Conductor also captures a baseline in `~/cache_dir/cluster_baseline_state.json` and reconciles cluster drift after each run.

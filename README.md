<div align="center">

<h1>SREGym: An AI-Native Platform for Benchmarking SRE Agents</h1>

[Overview](#🤖overview) | 
[🚀Quick Start](#🚀quickstart) |
[📦Installation](#📦installation) |
[⚙️Usage](#⚙️usage) |
[🤝Contributing](./CONTRIBUTING.md) |
[![Slack](https://img.shields.io/badge/-Slack-4A154B?style=flat-square&logo=slack&logoColor=white)](https://join.slack.com/t/SREGym/shared_invite/zt-3gvqxpkpc-RvCUcyBEMvzvXaQS9KtS_w)
</div>

<h2 id="overview">🤖 Overview</h2>

![SREGym Architecture Figure](./assets/SREGymFigure.png)

SREGym is a unified platform to enable the design, development, and evaluation of AI agents for Site Reliability Engineering (SRE). The core idea is to create live system environments for SRE agents to solve real-world problems.

SREGym also provides a comprehensive SRE benchmark suite with a wide variety of problems for evaluating SRE agents and for training next-generation AI agents.

### SRE Problems
Problems in SREGym consist of three components: an application, a fault, and an oracle. When evaluating a problem, SREGym first deploys the application specified in the problem. After deployment, the fault is injected into the system to cause the incident. Then, SREGym begins evaluating the agent and uses the oracle as the ground truth for the problem’s solution.

See our [registry]() for a complete list of problems.

SREGym is built to be extensible, we always welcome new contributions. See [CONTRIBUTING](./CONTRIBUTING.md) to get started.

<h2 id="📦installation">📦 Installation</h2>

### Requirements
- Python >= 3.12
- [Helm](https://helm.sh/)
- [brew](https://docs.brew.sh/Homebrew-and-Python)
- [kubectl](https://kubernetes.io/docs/tasks/tools/)
- [uv](https://github.com/astral-sh/uv)
- [kind](https://kind.sigs.k8s.io/) (if running locally)

### Recommendations
- [MCP Inspector](https://modelcontextprotocol.io/docs/tools/inspector) to test MCP tools.
- [k9s](https://k9scli.io/) to observe the cluster.

```bash
git clone --recurse-submodules https://github.com/SREGym/SREGym
cd SREGym
uv sync
uv run pre-commit install
```

<h2 id="🚀quickstart">🚀 Quickstart</h2>

## Setup your cluster
Choose either a) or b) to set up your cluster and then proceed to the next steps.

### a) Kubernetes Cluster (Recommended)
SREGym supports any kubernetes cluster that your `kubectl` context is set to, whether it's a cluster from a cloud provider or one you build yourself. 

We have an Ansible playbook to setup clusters on providers like [CloudLab](https://www.cloudlab.us/) and our own machines. Follow this [README](./scripts/ansible/README.md) to set up your own cluster.

### b) Emulated cluster
SREGym can be run on an emulated cluster using [kind](https://kind.sigs.k8s.io/) on your local machine. However, not all problems are supported.

```bash
# For x86 machines
kind create cluster --config kind/kind-config-x86.yaml

# For ARM machines
kind create cluster --config kind/kind-config-arm.yaml
```

<h2 id="⚙️usage">⚙️ Usage</h2>

### Running an Agent

#### Quick Start

To get started with the included Stratus agent:

1. Create your `.env` file:
```bash
mv .env.example .env
```

2. Open the `.env` file and configure your model and API key.

3. Run the benchmark:
```bash
python main.py
```

#### Agent Registration

SREGym uses [`agents.yaml`](./agents.yaml) to register agents for execution. This is how SREGym knows which agent to run when you start the benchmark. The Stratus agent is already registered:

```yaml
agents:
- name: stratus
  kickoff_command: python -m clients.stratus.stratus_agent.driver.driver --server http://localhost:8000
  kickoff_workdir: .
  kickoff_env: null
```

**To register your own agent:**
- `name`: A unique identifier for your agent
- `kickoff_command`: The command SREGym will execute to start your agent
- `kickoff_workdir`: The working directory from which to run the command
- `kickoff_env`: Optional environment variables (use `null` if none needed)

Add a new entry to `agents.yaml` following this format to register your custom agent.

#### Understanding Evaluation Phases

There are at most 4 phases in each problem of SREGym:

1. **NO-OP Detection**: We have deployed the application, but there is no incident happening. The agent should detect no incident in the cluster. After agent submission for this problem, the fault is injected.

   **Expected submission**: "Yes" or "No" to indicate incident.

2. **Incident Detection**: We've injected a fault into the cluster, it is now experiencing an incident.

   **Expected submission**: "Yes" or "No" to indicate incident.

3. **Fault Localization**: The agent should localize where the incident originates.

   **Expected submission**: The UID(s) of the resource where the incident originates.

4. **Incident Mitigation**: The agent should try to mitigate the incident and bring the cluster back online.

   **Expected submission**: No arguments for mitigation problems. *NOTE*: Not all problems are evaluated for mitigation.

#### Configuring Task Lists

By default, SREGym runs the common evaluation with all available problems and tasks. If you want to run a **custom evaluation** with a specific subset of problems or tasks, you can configure this using [`tasklist.yaml`](./SREGym/conductor/tasklist.yml).

The task list follows this format for each problem:
```yaml
k8s_target_port-misconfig:
  - detection
  - localization
  - mitigation
```

To create a custom evaluation, edit `tasklist.yaml` and specify which problems and tasks you want to run. For each problem (identified by `problem_id`), list any combination of `detection`, `localization`, or `mitigation` tasks (in this order). The `noop` phase is automatically included as the starting stage.

**Note:** If no entry exists for a problem in `tasklist.yaml`, all tasks will run by default. Additionally, `localization` and `mitigation` may be skipped if the problem does not have a corresponding oracle attached.

### MCP Tools

The benchmark is driven by agent submissions via the `submit` MCP tool. Each submission advances the benchmark to the next phase. To test your agent, run [`main.py`](https://github.com/SREGym/SREGym/blob/main/main.py) to start the benchmark, then have your agent submit answers at each phase.

SREGym provides a suite of MCP tools that enable agents to interact with the cluster and benchmark:

**Observability Tools:**
- `get_services`: Retrieve the list of service names from Jaeger
- `get_operations`: Query available operations for a specific service from Jaeger
- `get_traces`: Get Jaeger traces for a given service in the last n minutes
- `get_metrics`: Query real-time metrics data from Prometheus using PromQL expressions

**Cluster Management Tools:**
- `exec_kubectl_cmd_safely`: Execute kubectl commands against the Kubernetes cluster. Converts natural language to kubectl commands and executes them. Can get/describe/edit Kubernetes deployments, services, and other components. Takes one query at a time and requires namespace names for most queries
- `exec_read_only_kubectl_cmd`: Execute read-only kubectl commands (e.g., get, describe, logs, top, events). A restricted version of `exec_kubectl_cmd_safely` that only allows non-destructive operations
- `rollback_command`: Roll back the last kubectl command executed with `exec_kubectl_cmd_safely`
- `get_previous_rollbackable_cmd`: Get a list of previously executed commands that can be rolled back. When calling `rollback_command` multiple times, commands are rolled back in the order of this list

**Benchmark Interaction:**
- `submit`: Submit task results to the benchmark to progress to the next phase

The Stratus agent in [`clients/stratus`](https://github.com/SREGym/SREGym/tree/main/clients/stratus) demonstrates usages of these MCP tools in an agent.

### Monitoring with Dashboard

SREGym provides a dashboard to monitor the status of your evaluation. The dashboard runs automatically when you start the benchmark with `python main.py` and can be accessed at `http://localhost:11451` in your web browser.

## Acknowledgements
This project is generously supported by a Slingshot grant from the [Laude Institute](https://www.laude.org/).

## License
Licensed under the [MIT](LICENSE.txt) license.

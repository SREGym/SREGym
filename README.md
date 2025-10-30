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

#### Problem example
```python
class K8STargetPortMisconfig(Problem):
    def __init__(self, faulty_service="user-service"):
        app = SocialNetwork() # Select application
        super().__init__(app=app, namespace=app.namespace)

        self.faulty_service = faulty_service
        self.kubectl = KubeCtl()

        # === Attach evaluation oracles ===
        self.localization_oracle = LocalizationOracle(problem=self, expected=[faulty_service])
        self.mitigation_oracle = TargetPortMisconfigMitigationOracle(problem=self)
        
        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self): # Inject fault
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._inject(
            fault_type="misconfig_k8s",
            microservices=[self.faulty_service],
        )
```

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
git clone --recurse-submodules https://github.com/xlab-uiuc/SREGym
cd SREGym
uv sync
uv run pre-commit install
```

<h2 id="🚀quickstart">🚀 Setup Your Cluster</h2>

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

SREGym can be used in the following ways:
- [Evaluating agents on SREGym](#run-agent-on-SREGym)
- [Add new problems to SREGym](#how-to-add-new-problems-to-SREGym)
- [Add new applications to SREGym](#how-to-add-new-applications-to-SREGym)

### Evaluate agent on SREGym

#### Run the Stratus agent

To start, first create your `.env`:
```bash
mv .env.example .env
```

Then, select your model and paste your API key.

Finally:
```bash
python main.py
```

### Dashboard
SREGym provides a dashboard to monitor the status of your evaluation.

The dashboard runs automatically when you start the benchmark with `python main.py`.

You can access the dashboard at `http://localhost:11451` in your web browser.

#### Evaluating agents on SREGym
There are at most 4 phases in each problem of SREGym:
1. **NOOP Detection**: We have deployed the application, but there is no incident happening. The agent should detect no incident in the cluster. After agent submission for this problem, the fault is injected.
   
   **Expected submission**: "Yes" or "No" to indicate incident.
2. **Incident Detection**: We've injected a fault into the cluster, it is now experiencing an incident.

   **Expected submission**: "Yes" or "No" to indicate incident.
3. **Fault Localization**: The agent should localize where the incident originates.

   **Expected submission**: The UID(s) of the resource where the incident originates.
4. **Incident Mitigation**: The agent should try to mitigate the incident and bring the cluster back online.

   **Expected submission**: empty submission to indicate that the agent is satisfied with the cluster.

### Task list
SREGym will evaluate all problems and tasks in the task list (`tasklist.yaml`). By default, it contains every problem and task, and follows this format for each problem:
```yaml
k8s_target_port-misconfig:
  - detection
  - localization
  - mitigation
```

To configure what tasks you want the conductor to run on a particular problem, edit its entry (identified by problem_id) in [`tasklist.yml`](./SREGym/conductor/tasklist.yml). Specify any task(s) of `detection`, `localization` or `mitigation` (in this order) to tell the conductor to run them. `noop` is automatically assumed to be the starting stage of a problem. If there is no entry for a problem, the conductor will assume that all tasks are to be run for that one. `localization` and `mitigation` may be skipped if there is corresponding oracle attached to the problem.

### MCP Tools

The benchmark is driven by agent submissions. The benchmark expects the agent to submit a `POST` HTTP API call to the `http://localhost:8000/submit` HTTP endpoint.
Each submission pushes the benchmark to the next phase.

Therefore, if you would like to test your agent on SREGym, simply run [`main.py`](https://github.com/xlab-uiuc/SREGym/blob/main/main.py) to start the benchmark,
then instruct your agent to submit answers with HTTP API call in each phase of the benchmark problem.

SREGym provides a suite of MCP tools that enable agents to interact with the cluster and benchmark:

Observability Tools:
- `get_services`: Retrieve the list of service names from Jaeger
- `get_operations`: Query available operations for a specific service from Jaeger
- `get_traces`: Get Jaeger traces for a given service in the last n minutes
- `get_metrics`: Query real-time metrics data from Prometheus using PromQL expressions

Cluster Management Tools:
- `exec_kubectl_cmd_safely`: Execute kubectl commands against the Kubernetes cluster. Converts natural language to kubectl commands and executes them. Can get/describe/edit Kubernetes deployments, services, and other components. Takes one query at a time and requires namespace names for most queries
- `exec_read_only_kubectl_cmd`: Execute read-only kubectl commands (e.g., get, describe, logs, top, events). A restricted version of `exec_kubectl_cmd_safely` that only allows non-destructive operations
- `rollback_command`: Roll back the last kubectl command executed with `exec_kubectl_cmd_safely`
- `get_previous_rollbackable_cmd`: Get a list of previously executed commands that can be rolled back. When calling `rollback_command` multiple times, commands are rolled back in the order of this list

Benchmark Interaction:
- `submit`: Submit task results to the benchmark to progress to the next phase

The Stratus agent in [`clients/stratus`](https://github.com/xlab-uiuc/SREGym/tree/main/clients/stratus)
demonstrates usages of these MCP tools in an agent.

## Acknowledgements
We thank the [Laude Institute](https://www.laude.org/) for supporting this project.

## License
Licensed under the [MIT](LICENSE.txt) license.

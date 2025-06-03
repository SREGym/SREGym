<div align="center">

<h1>SREArena</h1>

<!-- [🤖Overview](#🤖overview) |  -->
[🚀Quick Start](#🚀quickstart) | 
[📦Installation](#📦installation) | 
[⚙️Usage](#⚙️usage) | 
[📂Project Structure](#📂project-structure) |
<!-- [📄How to Cite](#📄how-to-cite) -->

<!-- [![ArXiv Link](https://img.shields.io/badge/arXiv-2501.06706-red?logo=arxiv)](https://arxiv.org/pdf/2501.06706)
[![ArXiv Link](https://img.shields.io/badge/arXiv-2407.12165-red?logo=arxiv)](https://arxiv.org/pdf/2407.12165) -->
</div>


SREArena is a holistic framework to enable the design, development, and evaluation of autonomous AIOps agents that, additionally, serve the purpose of building reproducible, standardized, interoperable and scalable benchmarks. SREArena can deploy microservice cloud environments, inject faults, generate workloads, and export telemetry data, while orchestrating these components and providing interfaces for interacting with and evaluating agents. 

Moreover, SREArena provides a built-in benchmark suite with a set of problems to evaluate AIOps agents in an interactive environment. This suite can be easily extended to meet user-specific needs. See the problem list [here](/srearena/conductor/problems/registry.py#L15).

<h2 id="📦installation">📦 Installation</h2>

### Requirements
- Python >= 3.12
- [Helm](https://helm.sh/)
- [brew](https://docs.brew.sh/Homebrew-and-Python)

Recommended installation:
```bash
brew install python@3.12 uv
```
https://github.com/astral-sh/uv
We recommend [uv](https://github.com/astral-sh/uv) for managing dependencies. You can also use a standard `pip install -e .` to install the dependencies.

```bash
git clone --recurse-submodules <CLONE_PATH_TO_THE_REPO>
cd SREArena
which python3.12 # finds the python interpreter path
uv venv -p <python_interpreter_path>
source .venv/bin/activate
uv sync
```

<h2 id="🚀quickstart">🚀 Quick Start </h2>

<!-- TODO: Add instructions for both local cluster and remote cluster -->
Choose either a) or b) to set up your cluster and then proceed to the next steps.

### a) Local simulated cluster
SREArena can be run on a local simulated cluster using [kind](https://kind.sigs.k8s.io/) on your local machine.

```bash
# For x86 machines
kind create cluster --config kind/kind-config-x86.yaml

# For ARM machines
kind create cluster --config kind/kind-config-arm.yaml
```

If you're running into issues, consider building a Docker image for your machine by following this [README](kind/README.md). Please also open an issue.

### [Tips]
If you are running SREArena using a proxy, beware of exporting the HTTP proxy as `172.17.0.1`. When creating the kind cluster, all the nodes in the cluster will inherit the proxy setting from the host environment and the Docker container. 

The `172.17.0.1` address is used to communicate with the host machine. For more details, refer to the official guide: [Configure Kind to Use a Proxy](https://kind.sigs.k8s.io/docs/user/quick-start/#configure-kind-to-use-a-proxy).

Additionally, Docker doesn't support SOCKS5 proxy directly. If you're using a SOCKS5 protocol to proxy, you may need to use [Privoxy](https://www.privoxy.org) to forward SOCKS5 to HTTP.

If you're running VLLM and the LLM agent locally, Privoxy will by default proxy `localhost`, which will cause errors. To avoid this issue, you should set the following environment variable:

```bash
export no_proxy=localhost
``` 

After finishing cluster creation, proceed to the next "Update `config.yml`" step.

### b) Remote cluster
SREArena supports any remote kubernetes cluster that your `kubectl` context is set to, whether it's a cluster from a cloud provider or one you build yourself. We have some Ansible playbooks to setup clusters on providers like [CloudLab](https://www.cloudlab.us/) and our own machines. Follow this [README](./scripts/ansible/README.md) to set up your own cluster, and then proceed to the next "Update `config.yml`" step.

### Update `config.yml`
```bash
cd srearena
cp config.yml.example config.yml
```
Update your `config.yml` so that `k8s_host` is the host name of the control plane node of your cluster. Update `k8s_user` to be your username on the control plane node. If you are using a kind cluster, your `k8s_host` should be `kind`. If you're running SREArena on cluster, your `k8s_host` should be `localhost`.

### Running agents
Human as the agent:

```bash
python3 cli.py
(srearena) $ start misconfig_app_hotel_res-detection-1 # or choose any problem you want to solve
# ... wait for the setup ...
(srearena) $ submit("Yes") # submit solution
```

Run GPT-4 baseline agent:

```bash
# Create a .env file in the project root (if not exists)
echo "OPENAI_API_KEY=<YOUR_OPENAI_API_KEY>" > .env
# Add more API keys as needed:
# echo "QWEN_API_KEY=<YOUR_QWEN_API_KEY>" >> .env
# echo "DEEPSEEK_API_KEY=<YOUR_DEEPSEEK_API_KEY>" >> .env

python3 clients/gpt.py # you can also change the problem to solve in the main() function
```

The clients will automatically load API keys from your .env file.

You can check the running status of the cluster using [k9s](https://k9scli.io/) or other cluster monitoring tools conveniently.

To browse your logged `session_id` values in the W&B app as a table:

1. Make sure you have W&B installed and configured.
2. Set the USE_WANDB environment variable:
    ```bash
    # Add to your .env file
    echo "USE_WANDB=true" >> .env
    ```
3. In the W&B web UI, open any run and click Tables → Add Query Panel.
4. In the key field, type `runs.summary` and click `Run`, then you will see the results displayed in a table format.

<h2 id="⚙️usage">⚙️ Usage</h2>

SREArena can be used in the following ways:
- [Onboard your agent to SREArena](#how-to-onboard-your-agent-to-srearena)
- [Add new applications to SREArena](#how-to-add-new-applications-to-srearena)
- [Add new problems to SREArena](#how-to-add-new-problems-to-srearena)


### How to onboard your agent to SREArena?

SREArena makes it extremely easy to develop and evaluate your agents. You can onboard your agent to SREArena in 3 simple steps:

1. **Create your agent**: You are free to develop agents using any framework of your choice. The only requirements are:
    - Wrap your agent in a Python class, say `Agent`
    - Add an async method `get_action` to the class:

        ```python
        # given current state and returns the agent's action
        async def get_action(self, state: str) -> str:
            # <your agent's logic here>
        ```

2. **Register your agent with SREArena**: You can now register the agent with SREArena's conductor. The conductor will manage the interaction between your agent and the environment:

    ```python
    from srearena.conductor import Conductor

    agent = Agent()             # create an instance of your agent
    orch = Conductor()       # get SREArena's conductor
    orch.register_agent(agent)  # register your agent with SREArena
    ```

3. **Evaluate your agent on a problem**:

    1. **Initialize a problem**: SREArena provides a list of problems that you can evaluate your agent on. Find the list of available problems [here](/srearena/conductor/problems/registry.py) or using `orch.problems.get_problem_ids()`. Now initialize a problem by its ID: 

        ```python
        problem_desc, instructs, apis = orch.init_problem("k8s_target_port-misconfig-mitigation-1")
        ```
    
    2. **Set agent context**: Use the problem description, instructions, and APIs available to set context for your agent. (*This step depends on your agent's design and is left to the user*)


    3. **Start the problem**: Start the problem by calling the `start_problem` method. You can specify the maximum number of steps too:

        ```python
        import asyncio
        asyncio.run(orch.start_problem())
        ```

This process will create a [`Session`](/srearena/session.py) with the conductor, where the agent will solve the problem. The conductor will evaluate your agent's solution and provide results (stored under `data/results/`). You can use these to improve your agent.


### How to add new applications to SREArena?

SREArena provides a default [list of applications](/srearena/service/apps/) to evaluate agents for operations tasks. However, as a developer you can add new applications to SREArena and design problems around them.

> *Note*: for auto-deployment of some apps with K8S, we integrate Helm charts (you can also use `kubectl` to install as [HotelRes application](/srearena/service/apps/hotelres.py)). More on Helm [here](https://helm.sh).

To add a new application to SREArena with Helm, you need to:

1. **Add application metadata**
    - Application metadata is a JSON object that describes the application.
    - Include *any* field such as the app's name, desc, namespace, etc.
    - We recommend also including a special `Helm Config` field, as follows:

        ```json
        "Helm Config": {
            "release_name": "<name for the Helm release to deploy>",
            "chart_path": "<path to the Helm chart of the app>",
            "namespace": "<K8S namespace where app should be deployed>"
        }
        ```
        > *Note*: The `Helm Config` is used by the conductor to auto-deploy your app when a problem associated with it is started.

        > *Note*: The conductor will auto-provide *all other* fields as context to the agent for any problem associated with this app.

    Create a JSON file with this metadata and save it in the [`metadata`](/srearena/service/metadata) directory. For example the `social-network` app: [social-network.json](/srearena/service/metadata/social-network.json)

2. **Add application class**

    Extend the base class in a new Python file in the [`apps`](/srearena/service/apps) directory:

    ```python
    from srearena.service.apps.base import Application

    class MyApp(Application):
        def __init__(self):
            super().__init__("<path to app metadata JSON>")
    ```

    The `Application` class provides a base implementation for the application. You can override methods as needed and add new ones to suit your application's requirements, but the base class should suffice for most applications.



### How to add new problems to SREArena?

Similar to applications, SREArena provides a default [list of problems](/srearena/conductor/problems/registry.py) to evaluate agents. However, as a developer you can add new problems to SREArena and design them around your applications.

Each problem in SREArena has 5 components:
1. *Application*: The application on which the problem is based.
2. *Task*: The AIOps task that the agent needs to perform.
 Currently we support: [Detection](/srearena/conductor/tasks/detection.py), [Localization](/srearena/conductor/tasks/localization.py), [Analysis](/srearena/conductor/tasks/analysis.py), and [Mitigation](/srearena/conductor/tasks/mitigation.py).
3. *Fault*: The fault being introduced in the application.
4. *Workload*: The workload that is generated for the application.
5. *Evaluator*: The evaluator that checks the agent's performance.

To add a new problem to SREArena, create a new Python file 
in the [`problems`](/srearena/conductor/problems) directory, as follows:

1. **Setup**. Import your chosen application (say `MyApp`) and task (say `LocalizationTask`):

    ```python
    from srearena.service.apps.myapp import MyApp
    from srearena.conductor.tasks.localization import LocalizationTask
    ```

2. **Define**. To define a problem, create a class that inherits from your chosen `Task`, and defines 2 methods:, `inject_fault`, and `eval`:

    ```python
    class MyProblem(LocalizationTask):
        def __init__(self):
            self.app = MyApp()
        
        def inject_fault(self)
            # <your fault injection logic here>
        
        def eval(self, soln, trace, duration):
            # <your evaluation logic here>
    ```

3. **Register**. Finally, add your problem to the conductor's registry [here](/srearena/conductor/problems/registry.py).


See a full example of a problem [here](/srearena/conductor/problems/k8s_target_port_misconfig/target_port.py). 
<details>
  <summary>Click to show the description of the problem in detail</summary>

- **`inject_fault`**: Introduces a fault into the application. Use your own injector or SREArena's built-in one which you can also extend. E.g., a misconfig in the K8S layer:

    ```python
    from srearena.generators.fault.inject_virtual import *

    inj = VirtualizationFaultInjector(testbed="<namespace>")
    inj.inject_fault(microservices=["<service-name>"], fault_type="misconfig")
    ```

    > Relevant Code: [srearena/generators/fault](/srearena/generators/fault)


- **`eval`**: Evaluates the agent's solution using 3 params: (1) *soln*: agent's submitted solution if any, (2) *trace*: agent's action trace, and (3) *duration*: time taken by the agent.

    Here, you can use built-in default evaluators for each task and/or add custom evaluations. The results are stored in `self.results`:
    ```python
    def eval(self, soln, trace, duration) -> dict:
        super().eval(soln, trace, duration)     # default evaluation
        self.add_result("myMetric", my_metric(...))     # add custom metric
        return self.results
    ```

    > *Note*: When an agent starts a problem, the conductor creates a [`Session`](/srearena/session.py) object that stores the agent's interaction. The `trace` parameter is this session's recorded trace.

    > Relevant Code: [srearena/conductor/evaluators/](/srearena/conductor/evaluators/)

</details>




<h2 id="📂project-structure">📂 Project Structure</h2>

<summary><code>srearena</code></summary>
<details>
  <summary>Generators</summary>
  <pre>
  generators - the problem generators for srearena
  ├── fault - the fault generator organized by fault injection level
  │   ├── base.py
  │   ├── inject_app.py
  │  ...
  │   └── inject_virtual.py
  └── workload - the workload generator organized by workload type
      └── wrk.py - wrk tool interface
  </pre>
</details>

<details>
  <summary>Conductor</summary>
  <pre>
  conductor
  ├── conductor.py - the main orchestration engine
  ├── parser.py - parser for agent responses
  ├── evaluators - eval metrics in the system
  │   ├── prompts.py - prompts for LLM-as-a-Judge
  │   ├── qualitative.py - qualitative metrics
  │   └── quantitative.py - quantitative metrics
  ├── problems - problem definitions in srearena
  │   ├── k8s_target_port_misconfig - e.g., A K8S TargetPort misconfig problem
  │  ...
  │   └── registry.py
  ├── actions - actions that agents can perform organized by AIOps task type
  │   ├── base.py
  │   ├── detection.py
  │   ├── localization.py
  │   ├── analysis.py
  │   └── mitigation.py
  └── tasks - individual AIOps task definition that agents need to solve
      ├── base.py
      ├── detection.py
      ├── localization.py
      ├── analysis.py
      └── mitigation.py
  </pre>
</details>

<details>
  <summary>Service</summary>
  <pre>
  service
  ├── apps - interfaces/impl. of each app
  ├── helm.py - helm interface to interact with the cluster
  ├── kubectl.py - kubectl interface to interact with the cluster
  ├── shell.py - shell interface to interact with the cluster
  ├── metadata - metadata and configs for each apps
  └── telemetry - observability tools besides observer, e.g., in-memory log telemetry for the agent
  </pre>
</details>

<details>
  <summary>Observer</summary>
  <pre>
  observer
  ├── filebeat - Filebeat installation
  ├── logstash - Logstash installation
  ├── prometheus - Prometheus installation
  ├── log_api.py - API to store the log data on disk
  ├── metric_api.py - API to store the metrics data on disk
  └── trace_api.py - API to store the traces data on disk
  </pre>
</details>

<details>
  <summary>Utils</summary>
  <pre>
  ├── config.yml - srearena configs
  ├── config.py - config parser
  ├── paths.py - paths and constants
  ├── session.py - srearena session manager
  └── utils
      ├── actions.py - helpers for actions that agents can perform
      ├── cache.py - cache manager
      └── status.py - srearena status, error, and warnings
  </pre>
</details>

<summary><code>cli.py</code>: A command line interface to interact with SREArena, e.g., used by human operators.</summary>


<!-- <h2 id="📄how-to-cite">📄 How to Cite</h2>

```bibtex
@misc{chen2024aiopslab,
  title = {SREArena: A Holistic Framework to Evaluate AI Agents for Enabling Autonomous Clouds},
  author = {Chen, Yinfang and Shetty, Manish and Somashekar, Gagan and Ma, Minghua and Simmhan, Yogesh and Mace, Jonathan and Bansal, Chetan and Wang, Rujia and Rajmohan, Saravan},
  year = {2025},
  url = {https://arxiv.org/abs/2501.06706} 
}
@inproceedings{shetty2024building,
  title = {Building AI Agents for Autonomous Clouds: Challenges and Design Principles},
  author = {Shetty, Manish and Chen, Yinfang and Somashekar, Gagan and Ma, Minghua and Simmhan, Yogesh and Zhang, Xuchao and Mace, Jonathan and Vandevoorde, Dax and Las-Casas, Pedro and Gupta, Shachee Mishra and Nath, Suman and Bansal, Chetan and Rajmohan, Saravan},
  year = {2024},
  booktitle = {Proceedings of 15th ACM Symposium on Cloud Computing},
}
``` -->



## License
Licensed under the [MIT](LICENSE.txt) license.

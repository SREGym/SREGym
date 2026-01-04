<div align="center">

<h1>SREGym: A Benchmarking Platform for SRE Agents</h1>

[🔍Overview](#🤖overview) | 
[📦Installation](#📦installation) |
[🚀Quick Start](#🚀quickstart) |
[⚙️Usage](#⚙️usage) |
[🤝Contributing](./CONTRIBUTING.md) |
[📖Docs](https://sregym.com/docs) |
[![Slack](https://img.shields.io/badge/-Slack-4A154B?style=flat-square&logo=slack&logoColor=white)](https://join.slack.com/t/SREGym/shared_invite/zt-3gvqxpkpc-RvCUcyBEMvzvXaQS9KtS_w)
</div>

<h2 id="overview">🔍 Overview</h2>
SREGym is an AI-native platform to enable the design, development, and evaluation of AI agents for Site Reliability Engineering (SRE). The core idea is to create live system environments for SRE agents to solve real-world SRE problems. SREGym provides a comprehensive SRE benchmark suite with a wide variety of problems for evaluating SRE agents and also for training next-generation AI agents.
<br><br>

![SREGym Overview](/assets/SREGymFigure.png)

SREGym is inspired by our prior work on AIOpsLab and ITBench. It is architectured with AI-native usability and extensibility as first-class principles. The SREGym benchmark suites contain 86 different SRE problems. It supports all the problems from AIOpsLab and ITBench, and includes new problems such as OS-level faults, metastable failures, and concurrent failures. See our [problem set](https://sregym.com/problems) for a complete list of problems.


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
python main.py --agent <agent-name> --model <model-id>
```

For example, to run the Stratus agent:
```bash
python main.py --agent stratus --model gpt-4o
```

### Model Selection

SREGym supports multiple LLM providers. Specify your model using the `--model` flag:

```bash
python main.py --agent <agent-name> --model <model-id>
```

#### Available Models

| Model ID | Provider | Model Name | Required Environment Variables |
|----------|----------|------------|-------------------------------|
| `gpt-4o` | OpenAI | GPT-4o | `OPENAI_API_KEY` |
| `gemini-2.5-pro` | Google | Gemini 2.5 Pro | `GEMINI_API_KEY` |
| `claude-sonnet-4` | Anthropic | Claude Sonnet 4 | `ANTHROPIC_API_KEY` |
| `bedrock-claude-sonnet-4.5` | AWS Bedrock | Claude Sonnet 4.5 | `AWS_PROFILE`, `AWS_DEFAULT_REGION` |
| `moonshot` | Moonshot | Moonshot | `MOONSHOT_API_KEY` |
| `watsonx-llama` | IBM watsonx | Llama 3.3 70B | `WATSONX_API_KEY`, `WX_PROJECT_ID` |
| `glm-4` | GLM | GLM-4 | `GLM_API_KEY` |
| `azure-openai-gpt-4o` | Azure OpenAI | GPT-4o | `AZURE_API_KEY`, `AZURE_API_BASE` |

**Default:** If no model is specified, `gpt-4o` is used by default.

#### Examples

**OpenAI:**
```bash
# In .env file
OPENAI_API_KEY="sk-proj-..."

# Run with GPT-4o
python main.py --agent stratus --model gpt-4o
```

**Anthropic:**
```bash
# In .env file
ANTHROPIC_API_KEY="sk-ant-api03-..."

# Run with Claude Sonnet 4
python main.py --agent stratus --model claude-sonnet-4
```

**AWS Bedrock:**
```bash
# In .env file
AWS_PROFILE="bedrock"
AWS_DEFAULT_REGION=us-east-2

# Run with Claude Sonnet 4.5 on Bedrock
python main.py --agent stratus --model bedrock-claude-sonnet-4.5
```

**Note:** For AWS Bedrock, ensure your AWS credentials are configured via `~/.aws/credentials` and your profile has permissions to access Bedrock.

### Monitoring with Dashboard

SREGym provides a dashboard to monitor the status of your evaluation. The dashboard runs automatically when you start the benchmark with `python main.py` and can be accessed at `http://localhost:11451` in your web browser.

### Viewing Agent Traces with MLflow

SREGym uses MLflow to automatically capture detailed traces of agent execution. To view traces:

1. Start the MLflow UI:
```bash
mlflow ui
```

2. Open your browser and navigate to `http://localhost:5000`

3. Click on the **Experiments** tab to view all agent traces

The traces include LLM calls, tool executions, state transitions, and performance metrics for all agents.

## Acknowledgements
This project is generously supported by a Slingshot grant from the [Laude Institute](https://www.laude.org/).

## License
Licensed under the [MIT](LICENSE.txt) license.

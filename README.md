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
- [Docker](https://docs.docker.com/get-docker/)
- [Helm](https://helm.sh/) >= 4.0
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
uv run prek install
```

<h2 id="🚀quickstart">🚀 Quickstart</h2>

## Setup your cluster
Choose either a) or b) to set up your cluster and then proceed to the next steps.

### a) Kubernetes Cluster (Recommended)
SREGym supports any kubernetes cluster that your `kubectl` context is set to, whether it's a cluster from a cloud provider or one you build yourself.

We have an Ansible playbook to setup clusters on providers like [CloudLab](https://www.cloudlab.us/) and our own machines. Follow this [README](./scripts/ansible/README.md) to set up your own cluster.

### b) Emulated cluster
SREGym can be run on an emulated cluster using [kind](https://kind.sigs.k8s.io/) on your local machine. However, not all problems are supported.

**Note:** If you run into pod crashes or "too many open files" errors, see the [kind README](./kind/README.md) for required host kernel settings and troubleshooting.

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

1. Set your LLM API keys in the environment (required for your chosen model provider):
```bash
# OpenAI
export OPENAI_API_KEY="sk-proj-..."

# Anthropic
export ANTHROPIC_API_KEY="sk-ant-..."

# Google
export GEMINI_API_KEY="..."

# AWS Bedrock
export AWS_PROFILE="bedrock"
export AWS_DEFAULT_REGION="us-east-2"
```

2. Run the benchmark:
```bash
python main.py --agent stratus --model gpt-5
```

Use `--judge-model` to override the judge model separately (defaults to `--model`):
```bash
python main.py --agent stratus --model gpt-5 --judge-model claude-sonnet-4
```

#### Container Isolation

Agents always run in isolated Docker containers, preventing access to SREGym internals like problem definitions and grading logic. The image is built automatically on first run.

Use `--force-build` to rebuild the container image after updating dependencies or agent code:

```bash
python main.py --agent codex --model gpt-5 --force-build
```

### Model Selection

SREGym uses two model roles, both configurable via CLI:

| CLI Flag | Default | Purpose |
|----------|---------|---------|
| `--model` | `gpt-5` | Sets both agent and judge model |
| `--judge-model` | (same as `--model`) | Override just the judge evaluator model |

Make sure the required environment variables for your chosen provider are set before running. See the table below.

#### Available Models

| Model ID | Provider | Model Name | Required Environment Variables |
|----------|----------|------------|-------------------------------|
| `gpt-5` | OpenAI | GPT-5 | `OPENAI_API_KEY` |
| `gemini-2.5-pro` | Google | Gemini 2.5 Pro | `GEMINI_API_KEY` |
| `claude-sonnet-4` | Anthropic | Claude Sonnet 4 | `ANTHROPIC_API_KEY` |
| `bedrock-claude-sonnet-4.5` | AWS Bedrock | Claude Sonnet 4.5 | `AWS_PROFILE`, `AWS_DEFAULT_REGION` |


<details>
<summary><strong>Provider Examples</strong></summary>

**OpenAI:**
```bash
python main.py --agent stratus --model gpt-5
```

**Anthropic:**
```bash
python main.py --agent stratus --model claude-sonnet-4
```

**AWS Bedrock:**
```bash
python main.py --agent stratus --model bedrock-claude-sonnet-4.5
```

**Note:** For AWS Bedrock, ensure your AWS credentials are configured via `~/.aws/credentials` and your profile has permissions to access Bedrock.

</details>

## Acknowledgements
This project is generously supported by a Slingshot grant from the [Laude Institute](https://www.laude.org/).

https://github.com/user-attachments/assets/e7b2ee27-e7a9-436a-858d-ee58e8bbd61d

## License
Licensed under the [MIT](LICENSE.txt) license.

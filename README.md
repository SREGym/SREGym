<div align="center">

<h1>SREGym: A Benchmarking Platform for SRE Agents</h1>

[![Overview](https://img.shields.io/badge/%F0%9F%94%8D-Overview-blue?style=flat-square)](#overview)
[![Installation](https://img.shields.io/badge/%F0%9F%93%A6-Installation-blue?style=flat-square)](#📦installation)
[![Quick Start](https://img.shields.io/badge/%F0%9F%9A%80-Quick%20Start-blue?style=flat-square)](#🚀quickstart)
[![Usage](https://img.shields.io/badge/%E2%9A%99%EF%B8%8F-Usage-blue?style=flat-square)](#⚙️usage)
[![Contributing](https://img.shields.io/badge/%F0%9F%A4%9D-Contributing-blue?style=flat-square)](./CONTRIBUTING.md)
[![Docs](https://img.shields.io/badge/%F0%9F%93%96-Docs-blue?style=flat-square)](https://sregym.com/docs)
[![Leaderboard](https://img.shields.io/badge/%F0%9F%8F%86-Leaderboard-blue?style=flat-square)](https://sregym.com)
[![Slack](https://img.shields.io/badge/-Slack-4A154B?style=flat-square&logo=slack&logoColor=white)](https://join.slack.com/t/SREGym/shared_invite/zt-3gvqxpkpc-RvCUcyBEMvzvXaQS9KtS_w)
[![arXiv](https://img.shields.io/badge/arXiv-2605.07161-b31b1b?style=flat-square&logo=arxiv&logoColor=white)](https://arxiv.org/abs/2605.07161)
</div>

<h2 id="overview">🔍 Overview</h2>
SREGym is an AI-native platform to enable the design, development, and evaluation of AI agents for Site Reliability Engineering (SRE). The core idea is to create live system environments for SRE agents to solve real-world SRE problems. SREGym provides a comprehensive SRE benchmark suite with a wide variety of problems for evaluating SRE agents and also for training next-generation AI agents.
<br><br>

![SREGym Overview](/assets/overview.png)

SREGym is inspired by our prior work on AIOpsLab and ITBench. It is architectured with AI-native usability and extensibility as first-class principles. The SREGym benchmark suites contain 90 different SRE problems. It supports all the problems from AIOpsLab and ITBench, and includes new problems such as OS-level faults, metastable failures, and concurrent failures. See our [problem set](https://sregym.com/problems) for a complete list of problems.

SREGym has been used to simulate real-world cloud failures, such as:
- Cloudflare WAF regex rules exhausted CPU ([postmortem](https://blog.cloudflare.com/details-of-the-cloudflare-outage-on-july-2-2019), [simulation](https://github.com/SREGym/SREGym/pull/773))
- Admission webhook TLS mismatch ([postmortem](https://github.com/cert-manager/cert-manager/issues/6350), [simulation](https://github.com/SREGym/SREGym/pull/777))
- Exhausting conntrack table space crippled a production cluster ([postmortem](https://www.markbetz.net/2023/12/12/exhausting-conntrack-table-space-crippled-our-k8s-cluster), [simulation](https://github.com/SREGym/SREGym/pull/768))
- GKE ran out of IP addresses ([postmortem](https://deploy.live/blog/when-gke-ran-out-of-ip-addresses), [simulation](https://github.com/SREGym/SREGym/pull/774))
- Kafka poison pill ([postmortem](https://www.lydtechconsulting.com/blog/kafka-poison-pill), [simulation](https://github.com/SREGym/SREGym/pull/790))
- The Reddit Pi-Day Outage ([postmortem](https://www.reddit.com/r/RedditEng/comments/11xx5o0/you_broke_reddit_the_piday_outage/), [simulation](https://github.com/SREGym/SREGym/pull/828))

<h2 id="🚀SREGym-Lite">🚀🚀🚀 Start with SREGym-Lite</h2>

[SREGym-Lite](./docs/SREGym-Lite.md) is a curated set of 21 representative problems with varied difficulty levels that are friendly to run. It is the recommended starting point for new users and can run easily on a [Kind](https://kind.sigs.k8s.io/) setup with 8 vCPU and 16 GB of memory.


<h2 id="📦installation">📦 Installation</h2>

### Requirements
- Python >= 3.12
- [Docker](https://docs.docker.com/get-docker/)
- [Helm](https://helm.sh/docs/intro/install/) >= 4.0
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
SREGym runs on a self-managed Kubernetes cluster that you provision on Linux hosts you have SSH and root access to (e.g. [CloudLab](https://www.cloudlab.us/), bare-metal machines, or cloud VMs/VPS instances). We provide an Ansible playbook that builds the cluster for you. Follow this [README](./scripts/ansible/README.md) to set it up.

> [!NOTE]
> A managed Kubernetes service won't work out of the box, since SREGym's setup needs SSH and root access to the nodes for OS-level cluster configuration. Instead, spin up a few plain VMs/VPS instances and add them to `inventory.yml`.

### b) Emulated cluster
SREGym can be run on an emulated cluster using [kind](https://kind.sigs.k8s.io/) on your local machine. However, not all problems are supported.

**Note:** If you run into pod crashes or "too many open files" errors, see the [kind README](./kind/README.md) for required host kernel settings and troubleshooting.

```bash
# For x86 machines
bash kind/setup_kind_cluster.sh x86

# For ARM machines
bash kind/setup_kind_cluster.sh arm
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

2. Run the full benchmark:
```bash
uv run main.py --agent stratus --model gpt-5
```

Or start with SREGym-Lite:
```bash
uv run main.py --suite sregym-lite --agent stratus --model gpt-5
```

Use `--judge-model` to override the judge model separately (defaults to `--model`):
```bash
uv run main.py --agent stratus --model gpt-5 --judge-model anthropic/claude-sonnet-4-6-20250627
```

#### Container Isolation

Agents always run in isolated Docker containers, preventing access to SREGym internals like problem definitions and grading logic. The image is built automatically on first run.

Use `--force-build` to rebuild the container image after updating dependencies or agent code:

```bash
uv run main.py --agent codex --model gpt-5 --force-build
```

Containerized agents use filtered network access by default. SREGym allows the selected model provider and local SREGym
services. It blocks all other internet destinations. The allowlist is fixed before the provider preflight, so the agent
cannot add a new destination during the run. SREGym includes the authentication endpoint needed by supported subscription
clients, such as Codex. Set `AGENT_API_BASE` when Stratus, Codex, or a local OpenCode model uses a custom endpoint. An
unknown provider fails before the benchmark starts. Use `--internet-access open` only when the agent must have
unrestricted network access.

Use `--allow-agent-endpoint` to add a required destination without enabling all internet access. Repeat the option to
add more destinations:

```bash
uv run main.py --agent codex --model gpt-5.6-sol \
  --allow-agent-endpoint https://telemetry.example.com/v1
```

The URL permits its host, port, path, and child paths. A URL without a path permits all paths on that host and port.
This option has no effect with `--internet-access open`.

Filtered mode also blocks public internet access from application pods. It requires Calico 3.29 or later with policy tiers.
The kind setup script installs Calico 3.29.3. Older kind clusters need a Calico upgrade before filtered runs can start.
For an existing kind cluster, run:

```bash
kubectl apply --server-side --force-conflicts -f https://raw.githubusercontent.com/projectcalico/calico/v3.29.3/manifests/calico.yaml
kubectl rollout status daemonset/calico-node -n kube-system --timeout=240s
kubectl rollout status deployment/calico-kube-controllers -n kube-system --timeout=240s
kubectl get tiers.crd.projectcalico.org
```

The output must include the `adminnetworkpolicy` tier. These upgrade commands apply to the repository's kind setup, not custom Calico installations.

### Deployment Profiles

`--profile` controls how much infrastructure SREGym stands up. It is independent of
`--suite` — the profile selects *what gets deployed*, the suite selects *which problems run*.

| Profile | Behaviour |
|---------|-----------|
| `full` (default) | The standard stack. Use this for results you intend to compare against the leaderboard. |
| `svelte` | Additionally drops components that nothing in SREGym reads, and shortens metric retention. |

```bash
uv run main.py --suite sregym-lite --agent stratus --model gpt-5 --profile svelte
```

`svelte` removes:

- astronomy-shop's bundled **OpenSearch**, **Grafana** and **Jaeger**. Nothing in `sregym/`,
  `mcp_server/` or `clients/` queries OpenSearch or Grafana; the bundled Jaeger is deleted
  moments after deployment anyway, by `Jaeger.create_external_name_service()`.
- Prometheus **Alertmanager** and **Pushgateway** (no alert rules are configured and nothing
  pushes), and TSDB retention cut from 15d to 2h.
- The OpenEBS **node-disk-manager** stack, which backs the `openebs-device` StorageClass.
  SREGym only provisions through `openebs-hostpath`.

Measured on one astronomy-shop problem (peak RSS / peak CPU, sampled over the run):

| | `full` | `svelte` |
|---|---|---|
| OpenSearch | 1096 MiB / 1709m | — |
| Grafana | 475 MiB / 303m | — |
| OpenEBS NDM (7 pods) | ~190 MiB / 582m | — |

> [!WARNING]
> `svelte` changes what an agent can observe in the cluster, so its scores are **not**
> comparable with `full`. It is intended for local iteration on memory-constrained hosts,
> not for leaderboard submissions. Note that `--profile svelte` and `--suite sregym-lite`
> are unrelated: you can run either without the other.

### Model Selection

SREGym uses [LiteLLM](https://docs.litellm.ai/docs/providers) model strings directly (no config file needed). Just pass any supported model string via `--model`:

| CLI Flag | Default | Purpose |
|----------|---------|---------|
| `--model` | `gpt-5` | Sets both agent and judge model |
| `--judge-model` | (same as `--model`) | Override just the judge evaluator model |

Set the required environment variable for your provider before running:

| Provider | Model String Examples | Required Environment Variables |
|----------|----------------------|-------------------------------|
| OpenAI | `gpt-5`, `gpt-4o` | `OPENAI_API_KEY` |
| Anthropic | `anthropic/claude-sonnet-4-6-20250627` | `ANTHROPIC_API_KEY` |
| Google | `gemini/gemini-2.5-pro` | `GEMINI_API_KEY` |
| AWS Bedrock | `bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0` | `AWS_PROFILE`, `AWS_DEFAULT_REGION` |
| Azure | `azure/gpt-4o` | `AZURE_API_KEY`, `AZURE_API_BASE`, `AZURE_API_VERSION` |

#### Local LLMs

SREGym supports local models through Ollama and OpenAI-compatible servers such as vLLM and LM Studio. The examples below use Ollama.

Set `AGENT_API_KEY` as well if the endpoint requires authentication.

> [!CAUTION]
> When you use `--internet-access filtered`, the agent runs on an isolated Docker network. It cannot reach a local model server that listens only on `127.0.0.1` or `localhost`. Configure the server to listen on a host-reachable interface, such as `0.0.0.0`, and use `http://host.docker.internal:<port>` as the API base. Protect the exposed port with authentication or a firewall. This requirement is the same for Kind and external Kubernetes clusters because the connection is between the agent container and the machine running SREGym.

**Stratus with Ollama:**

```bash
ollama pull qwen3-coder:30b

export AGENT_API_BASE="http://127.0.0.1:11434"
uv run main.py --agent stratus --model ollama_chat/qwen3-coder:30b
```

**OpenCode with Ollama:**

OpenCode uses the endpoint's OpenAI-compatible `/v1` API.

```bash
export AGENT_API_BASE="http://127.0.0.1:11434/v1"
uv run main.py --agent opencode --model local/qwen3-coder:30b
```

When `--judge-model` is not set, SREGym reuses the agent model and endpoint for the judge. This works directly for Stratus because its model identifier is LiteLLM-compatible. For OpenCode, SREGym normalizes `local/<served-model>` to `openai/<served-model>` for the judge, because OpenCode's `local/` provider uses an OpenAI-compatible endpoint.

For vLLM, LM Studio, or another OpenAI-compatible server, point `AGENT_API_BASE` to its `/v1` endpoint and use `openai/<served-model>` with Stratus or `local/<served-model>` with OpenCode.

To use a different LiteLLM judge provider, pass `--judge-model` explicitly:

```bash
export JUDGE_API_BASE="http://127.0.0.1:11434"
uv run main.py --agent opencode --model local/qwen3-coder:30b --judge-model ollama_chat/qwen3-coder:30b
```

**Separate judge endpoint:**

Set `JUDGE_API_BASE` and `JUDGE_API_KEY` when the judge uses a different endpoint or credential:

```bash
export JUDGE_API_BASE="https://example.test/v1"
export JUDGE_API_KEY="..."
uv run main.py --agent stratus --model ollama_chat/qwen3-coder:30b --judge-model gpt-5
```

<details>
<summary><strong>Provider Examples</strong></summary>

**OpenAI:**
```bash
uv run main.py --agent stratus --model gpt-5
```

**Anthropic:**
```bash
uv run main.py --agent stratus --model anthropic/claude-sonnet-4-6
```

**Google:**
```bash
uv run main.py --agent stratus --model gemini/gemini-2.5-pro
```

**AWS Bedrock:**
```bash
uv run main.py --agent stratus --model bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0
```

**Note:** For AWS Bedrock, ensure your AWS credentials are configured via `~/.aws/credentials` and your profile has permissions to access Bedrock.

See the full list of supported providers and model strings in the [LiteLLM docs](https://docs.litellm.ai/docs/providers).

</details>

## Cite This
If our work is useful for you, please cite it:

```bibtex
@article{sregym:26,
  author  = {Jackson Clark and Yiming Su and Saad Mohammad Rafid Pial and Yifang Tian and Lily Gniedziejko and Hans-Arno Jacobsen and Yinfang Chen and Tianyin Xu},
  title   = {{SREGym: A Live Benchmark for AI SRE Agents with High-Fidelity Failure Scenarios}},
  journal = {arXiv:2605.07161},
  year    = {2026},
  month   = may,
  eprint  = {2605.07161},
  archivePrefix = {arXiv}
}
```

## Acknowledgements
This project is generously supported by a Slingshot grant from the [Laude Institute](https://www.laude.org).

https://github.com/user-attachments/assets/e7b2ee27-e7a9-436a-858d-ee58e8bbd61d

## License
Licensed under the [MIT](LICENSE.txt) license.

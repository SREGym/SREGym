## 🔍 Overview
SREGym is an AI-native platform to enable the design, development, and evaluation of AI agents for Site Reliability Engineering (SRE). The core idea is to create live system environments for SRE agents to solve real-world SRE problems. SREGym provides a comprehensive SRE benchmark suite with a wide variety of problems for evaluating SRE agents and also for training next-generation AI agents.

SREGym is inspired by our prior work on AIOpsLab and ITBench. It is architectured with AI-native usability and extensibility as first-class principles. The SREGym benchmark suites contain 86 different SRE problems. It supports all the problems from AIOpsLab and ITBench, and includes new problems such as OS-level faults, metastable failures, and concurrent failures. See our [problem set](https://sregym.com/problems) for a complete list of problems.

## 📦 Installation
Please make sure you have the dependencies below.
### Requirements
- Python == 3.12
- Go >= 1.24.1
- [Docker](https://docs.docker.com/get-docker/)
- [Helm](https://helm.sh/)
- [brew](https://docs.brew.sh/Homebrew-and-Python)
- [kubectl](https://kubernetes.io/docs/tasks/tools/)
- [uv](https://github.com/astral-sh/uv)
- [kind](https://kind.sigs.k8s.io/) 

After installing `uv` and `python3.12`, please run this command in the artifact root folder (`SREGym`) to install all Python dependencies:
```
uv sync
```

We also require edits on the kernel configurations. Add these lines to the bottom of `/etc/sysctl.conf`:

```
fs.inotify.max_user_instances = 512
fs.inotify.max_user_watches = 100000
```

Then apply the changes: `sudo sysctl -p`.
### Recommendations
- [MCP Inspector](https://modelcontextprotocol.io/docs/tools/inspector) to test MCP tools.
- [k9s](https://k9scli.io/) to observe the cluster.

## Artifact Evaluation Steps
We recommend using a machine with at least 16GB memory for local Kubernetes cluster emulation.

### 1. Download the artifact
```
git clone --recurse-submodules https://github.com/SREGym/SREGym
```
### 2. Start a local cluster
Navigate to `SREGym`, and start a `kind` cluster based on your machine's architecture:
```bash
# if on x86
kind create cluster --config kind/kind-config-x86.yaml
# if on Arm
kind create cluster --config kind/kind-config-arm.yaml
```

Wait until `kind` creates a local Kubernetes cluster on you machine. If you see:
```bash
Creating cluster "kind" ...
 ✓ Ensuring node image (jacksonarthurclark/aiopslab-kind-x86:latest) 🖼 
 ✓ Preparing nodes 📦 📦 📦 📦  
 ✓ Writing configuration 📜 
 ✓ Starting control-plane 🕹️ 
 ✓ Installing CNI 🔌 
 ✓ Installing StorageClass 💾 
 ✓ Joining worker nodes 🚜 
Set kubectl context to "kind-kind"
You can now use your cluster with:

kubectl cluster-info --context kind-kind

Have a question, bug, or feature request? Let us know! https://kind.sigs.k8s.io/#community 🙂
```
The cluster is ready!

### 3. Run a problem with the `demo` agent
To help the reviewer using SREGym, we create a demo SRE agent that deterministically runs a sequence of diagnosis and mitigation commands on the cluster, to avoid incurring high model credit cost. We also made small modifications in the diagnosis oracle to avoid using LLM-as-a-Judge and rely on string comparison, but this is only for a smooth demo experience.

In this demo, three problems are altered for a smooth demo evaluation experience with no LLM model credit cost. Here are their problem IDs and a short description.
1. `incorrect_image`: The success case problem described in the live demo video. This problem includes a fault where we misconfigured a microservice's container image, failing the pod that runs the service.
2. `capacity_decrease_rpc_retry_storm`: The metastable failure problem described in the demo paper. This problem reproduces a metastable failure, where the RPC retry policy is overly aggressive, causing a persistent RPC retry storm among the microservices.
3. `noisy_problem`: The failure case problem described in the live demo video. This problem injects two faults concurrently. We first inject a port misconfiguration fault into the `user-service` microservice in a Social Network microservice application. The fault causes `user-service` to listen on the wrong port for incoming request,  causing requests to fail. To mimic cluster noise, we inject a storage volume misconfiguration fault in the internal Jaeger Tracing service, which is invisible to the user experience.

Note that, due to limited access to the operating system kernel, we are unable to provide the disk fault problem described in the paper with this demo artifact. However, if the reviewers request, we are able to provide remote SSH access to machines configured for hardware failures on demand.

Let us walk through each problem.

#### `incorrect_image`
Before proceeding, please make sure a local Kubernetes cluster is created and ready.

The demo agent works by reading a file called `kubectl_cmds.txt` under the `SREGym/clients/demo` directory. In this file, it contains a sequence of commands that the demo agent will run against the cluster, with the `kubectl` command line interface. This is the only command line tool that the agent can use to make state changes in the cluster. It is the main tool the agent use to mitigate the incident.

For each problem included in the demo, we include a `<problem_name>_kubectl_cmd.txt` . It includes correct commands that an SRE agent should use to mitigate the incidents.

Before we proceed, make sure you run this command under `SREGym/clients/demo`:
```bash
mv incorrect_image_kubectl_cmds.txt kubectl_cmds.txt
```

Deploy the problem and the demo agent with this command:
```bash
uv run main.py --problem incorrect_image --agent demo
```

When the deployment is ready, you should see a key log that looks like this:
```
INFO - all.sregym.conductor - ✅ Deployment complete. Ready for submission. Current stage is: diagnosis
```
Right below it, you should also see some notes on the demo agent itself:
```
No handler found for root logger

**************************************************
DEMO AGENT ACTIVE (FILE-TRIGGER MODE)
Advance commands: /tmp/next
Skip commands:    /tmp/skip
Quit agent:       /tmp/quit
**************************************************


====================
WAITING FOR TRIGGER for command [1/4]: kubectl -n astronomy-shop get pods
```

In this example, the agent can observe the problem immediately by running `kubectl get pods`. Run `touch /tmp/next` to step into the next command for the demo agent. You should see this:
```
INFO - all.demo.driver - [Turn 1] Executing: kubectl -n astronomy-shop get pods
INFO - all.demo.driver - [Turn 1] Result: NAME                               READY   STATUS             RESTARTS   AGE
accounting-5fc6ddfb47-2r7sn        1/1     Running            0          33m
ad-6fbf6d78d4-jn8zp                1/1     Running            0          33m
cart-74587775fc-s58z5              1/1     Running            0          33m
checkout-6c769b88b5-l7vcf          1/1     Running            0          34m
currency-7b6cd9c94c-zhlqh          1/1     Running            0          34m
email-74f9c666b6-nhts2             1/1     Running            0          34m
flagd-7b7cbb4d4d-5rfxq             2/2     Running            0          33m
fraud-detection-859f4f4697-qxsg8   1/1     Running            0          33m
frontend-6859c775df-k6wj9          1/1     Running            0          33m
frontend-proxy-84f445bb4b-jg5dw    1/1     Running            0          33m
grafana-948cdf675-48vdv            4/4     Running            0          33m
image-provider-86b767dd45-jhq7r    1/1     Running            0          34m
jaeger-6d665c6cb8-qtrq7            1/1     Running            0          33m
kafka-7c8b4f5978-v24rc             1/1     Running            0          34m
llm-74d9597c76-qm9zx               1/1     Running            0          34m
load-generator-6c47b4fcd5-ztwp9    1/1     Running            0          33m
locust-fetcher                     1/1     Running            0          33m
opensearch-0                       1/1     Running            0          34m
otel-collector-agent-cbrgc         1/1     Running            0          33m
otel-collector-agent-g7wfg         1/1     Running            0          33m
otel-collector-agent-k27lf         1/1     Running            0          33m
payment-556cf8d457-552fz           1/1     Running            0          34m
postgresql-6bd9b846cb-gtlj4        1/1     Running            0          33m
product-catalog-7fdc8cd8df-nc5jk   0/1     ImagePullBackOff   0          32m
product-reviews-7bccd49cb7-8tnf4   1/1     Running            0          33m
quote-7c77885dc4-rfh8b             1/1     Running            0          34m
recommendation-5bb649dccb-xpbck    1/1     Running            0          33m
shipping-6d777796df-7zdl8          1/1     Running            0          34m
valkey-cart-cbcc554f6-hdvf8        1/1     Running            0          34m
```

As you can see, the pod `product-catalog-7fdc8cd8df-nc5jk` in the `ImagePullBackOff` due to the incorrect image name. The next command runs a `POST` HTTP API call to the `submit` endpoint, for the demo agent to submit its result for the diagnosis phase. The mock LLM-as-a-Judge should judge the result as correct:
```
== Mock LLM-as-a-Judge Evaluation ==
✅ Correct diagnosis: True
```

The next command mitigates the incident by pointing the microservice deployment to the correct Docker image. Run `touch /tmp/next`. You should see:
```
INFO - all.demo.driver - [Turn 3] Executing: kubectl -n astronomy-shop patch deployment product-catalog --type='strategic' --patch "{\"spec\": {\"template\": {\"spec\": {\"containers\": [{\"name\": \"product-catalog\", \"image\": \"ghcr.io/open-telemetry/demo:latest-product-catalog\"}]}}}}"
INFO - all.demo.driver - [Turn 3] Result: deployment.apps/product-catalog patched
```
If we run `touch /tmp/next` again, we will see that SREGym judges the mitigation phase as correct as well:
```
== Mitigation Evaluation ==
✅ Deployment product-catalog using correct image: ghcr.io/open-telemetry/demo:latest-product-catalog
```
At this point, this problem is finished. The benchmark enters a shutdown phase to clean up any resources used.

**Observability MCP Servers**
Besides `kubectl`, the agent also has access to observability MCP servers to help it diagnose the incident. If you would like to explore the MCP servers, please make sure you have `npx` installed. Then, run:
```
npx @modelcontextprotocol/inspector
```
To run the MCP server inspector. Please refer to [the official guide](https://modelcontextprotocol.io/docs/tools/inspector) for its features. To connect to SREGym's MCP servers, here are the endpoints:
1. Jaeger Tracing: `http://127.0.0.1:9954/jaeger/sse`
2. Prometheus Metrics: `http://127.0.0.1:9954/prometheus/sse`

`kubectl` and `submit()` endpoints are also provided as MCP servers:
1. `kubectl` MCP server: `http://127.0.0.1:9954/kubectl/sse`
2. Submission MCP server: `http://127.0.0.1:8000/submit_mcp/sse`
#### `capacity_decrease_rpc_retry_storm`
Before proceeding, please make sure a local Kubernetes cluster is created and ready. For more background on metastable problems, we recommend [*Metastable Failures in Distributed Systems*](https://sigops.org/s/conferences/hotos/2021/papers/hotos21-s11-bronson.pdf), authored by Bronson et al.

Please make sure you run this command under `SREGym/clients/demo`:
```bash
mv capacity_decrease_rpc_retry_storm_kubectl_cmds.txt kubectl_cmds.txt
```

Deploy the problem and the demo agent with this command:
```bash
uv run main.py --problem capacity_decrease_rpc_retry_storm --agent demo
```

In this problem, the metastable behavior is triggered by overly aggressive RPC retry policies. The actual fault we injected is setting retry timeout to `30ms` and retry count to `30`. Thus, when the RPCs time out, it causes a retry storm throughout the cluster, affecting all services.

Please step through the demo agent commands similarly as the `incorrect_image` problem.

Due to the nature of metastable behavior, fixing the root cause (i.e., trigger) is not enough. All related services must be restarted to erase the metastable behavior and let the system start afresh. The mitigation commands by the demo agent reset the RPC retry policy and restarts all microservices that use this RPC retry policy.

#### `noisy_problem` or Concurrent Faults

Before proceeding, please make sure a local Kubernetes cluster is created and ready.

Please make sure you run this command under `SREGym/clients/demo`:
```bash
mv noisy_problem_kubectl_cmds.txt kubectl_cmds.txt
```

Deploy the problem and the demo agent with this command:
```bash
uv run main.py --problem capacity_decrease_rpc_retry_storm --agent demo
```

Same as other two problems, please step through the commands to replay this agent trace. In this problem, with SREGym's modular design, we deploy two faults concurrently. We cause both the `user-service` microservice in the Social Network microservice application and the internal observability service Jaeger tracing to fail. More specifically, we inject a port misconfiguration to fail requests coming to `user-service`, and a scheduler misconfiguration to fail the tracing service. Because the failing `user-service` directly impacts user experience, we expect the SRE agent to prioritize on fixing the `user-service`. 

However, in practice, the agent takes a greedy approach in mitigating incidents. Because the scheduler misconfiguration directly manifests on the pod-level, the agent prioritizes on fixing that and ignores the network misconfiguration fault.

The commands included in `noisy_problem_kubectl_cmds.txt` shows such a failure case and only focus on the scheduler misconfiguration as mitigation. The solution to the scheduler misconfiguration is to downscale the Jaeger tracing deployment to 1 replica. By running through the commands, you will see that in both diagnosis and mitigation evaluation phases, the agent fails.


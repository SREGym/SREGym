# {{dataset_title}}

[SREGym](https://github.com/SREGym/SREGym) is a live benchmark for AI site
reliability engineering (SRE) agents. Each task deploys a real microservice
application on a private Kubernetes cluster and injects a failure modeled on
production incidents. The agent must then mitigate it using `kubectl`.

{{dataset_summary}}

## Run

```bash
harbor run -d {{dataset_name}} -a claude-code -m anthropic/<model> -e daytona -n 4
```

Each task starts a privileged `sregym` sidecar. The sidecar runs its own
Docker daemon and a four-node KIND cluster, deploys the application, captures a
healthy baseline and injects the fault. Only then does the agent start. The
agent works in a separate container and reaches the cluster through a
Kubernetes API proxy that hides SREGym's own infrastructure. It never sees the
problem ID or grading code.

### Requirements

- **A Harbor environment that runs Docker Compose with privileged services:**
  - expected to work: `docker`, `ec2`, `gke` (Standard), `daytona`, and
    VM-based sandboxes
  - won't work: Modal's default gVisor runtime and `beam`
- **About {{cpus}} CPUs, {{memory_mb}} MiB of memory and {{storage_mb}} MiB of
  disk per task.** On the local `docker` environment, these limits apply to the
  agent container only.
- **Network access for the sidecar** to container registries and Helm chart
  repositories during setup. Setup takes 5 to 30 minutes per task.

## Grading

The reward is binary. It is 1.0 when the problem's mitigation oracle finds the
application healthy after the agent stops, and 0.0 otherwise. The oracle runs
inside the sidecar against the baseline it captured before the fault. Harbor
stops the agent container first, so the agent cannot interfere with grading.

SREGym's LLM-judged diagnosis stage is not part of this dataset.

## Reference solutions

Each task's `solution/solve.sh` asks the sidecar to run the problem's own
recovery. That endpoint needs a secret held by the SREGym maintainers, so a
published task cannot be used to bypass the agent's work. To validate tasks
with Harbor's oracle agent, generate your own copy of the dataset with the
[adapter](https://github.com/SREGym/SREGym/blob/main/docs/harbor.md).

## Tasks

| Task | Application |
|---|---|
{{task_rows}}

## Citation

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

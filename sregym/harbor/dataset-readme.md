# {{dataset_title}}

[SREGym](https://github.com/SREGym/SREGym) is a live benchmark for AI site
reliability engineering (SRE) agents. Each task deploys a real microservice
application on a private Kubernetes cluster and injects a failure modeled on
production incidents. The agent must then mitigate it using `kubectl`.

{{dataset_summary}}

## Run

On plain Docker (Harbor's `docker` environment), save this overlay as
`sregym-docker.yaml`. It lifts Docker's default seccomp profile and masked
`/proc` paths so the task can create its cluster's namespaces; it grants no
privileges or capabilities.

```yaml
services:
  main:
    security_opt:
      - seccomp=unconfined
      - systempaths=unconfined
      - apparmor=unconfined
    cgroup: private
    ulimits:
      nofile:
        soft: 1048576
        hard: 1048576
```

```bash
harbor run -d {{dataset_name}} -a claude-code -m anthropic/<model> -n 4 \
    -e docker --extra-docker-compose "$PWD/sregym-docker.yaml" --cpus limit --memory limit
```

Each task is one unprivileged container. It starts a private four-node
Kubernetes cluster, deploys the application, captures a healthy baseline and
injects the fault. Only then does the agent start. The agent runs as an
unprivileged user and reaches the cluster through a Kubernetes API proxy that
hides SREGym's own infrastructure. It cannot read the problem ID or the grading
code.

### Requirements

- **A Harbor environment that lets the container's root create namespaces and
  mount cgroups**, without `--privileged`:
  - Harbor's `docker` environment, with the overlay above. On Ubuntu 24.04
    hosts, also run `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`.
  - Sysbox sandboxes (`daytona`) and VM-based sandboxes, without the overlay.
    Daytona refuses more than 4 CPUs / 8 GiB per sandbox unless its limits are
    raised.
- **About {{cpus}} CPUs, {{memory_mb}} MiB of memory and {{storage_mb}} MiB of
  disk per task.**
- **Network access** to container registries and Helm chart repositories during
  setup, which takes several minutes per task.

## Grading

The reward is binary. It is 1.0 when the problem's mitigation oracle finds the
application healthy after the agent stops, and 0.0 otherwise. The oracle runs in
the task container against the baseline it captured before the fault. The
verifier kills the agent's processes before grading, and grading needs a token
only root can read, so the agent cannot interfere with it.

SREGym's LLM-judged diagnosis stage is not part of this dataset.

## Reference solutions

Each task's `solution/solve.sh` asks SREGym's backend to run the problem's own
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

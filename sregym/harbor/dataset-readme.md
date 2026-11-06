# {{dataset_title}}

[SREGym](https://github.com/SREGym/SREGym) is a live benchmark for AI site
reliability engineering (SRE) agents. Each task deploys a real microservice
application on a private Kubernetes cluster and injects a failure modeled on
production incidents. The agent must then mitigate it using `kubectl`.

{{dataset_summary}}

## Run

```bash
harbor run -d {{dataset_name}} -a claude-code -m anthropic/<model> -e docker
```

Each task is one container. It starts a private four-node Kubernetes cluster,
deploys the application, captures a healthy baseline and injects the fault. Only
then does the agent start, which takes several minutes. The agent runs as an
unprivileged user and reaches the cluster through a Kubernetes API proxy that
hides SREGym's own infrastructure. It cannot read the problem ID or the grading
code. A task uses up to {{cpus}} CPUs, {{memory_mb}} MiB of memory and
{{storage_mb}} MiB of disk; pass `-n` to run several at once.

Each task ships the Docker settings its cluster needs
(`environment/docker-compose.yaml`): two added capabilities, but not
`--privileged`. If the Docker host cannot run the cluster, setup stops with an
explanation before the agent starts, and Harbor records an error rather than a
score.

## Sandbox requirements

Each task needs a sandbox of at least **{{cpus}} CPUs, {{memory_mb}} MiB of
memory and {{storage_mb}} MiB of disk**. A smaller sandbox
either fails at creation or starts a cluster that never becomes ready, and the
task then fails after the healthcheck times out. Plan for a sandbox at these
sizes, not the defaults.

- **Open files:** the sandbox must allow a container open-file limit of
  `524288`, which the shipped compose file requests. A lower hard limit makes
  `docker compose up` fail with `operation not permitted`.
- **Inotify (local Docker):** every node in a cluster uses inotify instances, and
  they count against the host's per-user limit. The default
  `fs.inotify.max_user_instances=128` runs out with a few concurrent tasks, which
  crash-loops the cluster's pods. Set it to at least `1024` (see
  [`kind/README.md`](https://github.com/SREGym/SREGym/blob/main/kind/README.md)).
- **Concurrency:** setup takes a long time, roughly 30–45 minutes per task on a
  64-core host, and each task uses about 10 GB of Docker volumes. Start with a
  low `-n` and watch disk space before raising it.

Platforms we tested, and what went wrong:

- **Daytona:** on our account, sandboxes were capped at 4 CPUs, 8 GiB of memory
  and 10 GB of disk, so these tasks could not start. Account limits are set per
  organization, so check yours before you run.
- **E2B:** on our account, the template memory limit was 8192 MiB, so these tasks
  could not be built at full size. Check your team's limit before you run.
- **Local Docker:** one task passed the oracle on a 64-core, 125 GiB host.
  Concurrent runs need the inotify setting above.

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

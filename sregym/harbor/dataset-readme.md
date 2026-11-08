# {{dataset_title}}

[SREGym](https://github.com/SREGym/SREGym) is a live benchmark for AI site
reliability engineering (SRE) agents. Each task deploys a real microservice
application on a Kubernetes cluster and injects a failure modeled on
production incidents.

{{dataset_summary}}

## Run

```bash
harbor run -d {{dataset_name}} -a claude-code -m anthropic/<model> -e docker
```

Each task runs a four-node Kubernetes cluster in one container. Setup takes
several minutes before the agent starts.

## Sandbox size

Each task needs at least **{{cpus}} CPUs, {{memory_mb}} MiB of memory and
{{storage_mb}} MiB of disk**, and an open-file limit of 524288. On local Docker,
set `fs.inotify.max_user_instances` to at least 1024 when running tasks
concurrently.

## Grading

The reward is 1.0 if the application is healthy after the agent stops, and 0.0
otherwise.

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

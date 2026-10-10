# Chaos-agent problem generation

SREGym's handcrafted problems are close to saturation. This pipeline generates new,
mitigation-only problems automatically: a **chaos agent** breaks a healthy
deployment, the fault is replayed and checked by programmatic validity gates, and
only faults that a **panel of fixer agents** mostly fails to mitigate within the
standard 30 minutes are kept.

## Design

| Concern | Mechanism |
| --- | --- |
| Same access as the fixer | The chaos agent is an ordinary `codex`/`claudecode` agent run against the `chaos_canvas_<app>` problem (no fault). The conductor sets `chaos: true` in `/get_app`, and the drivers swap in the chaos instruction (`clients/chaos/prompt.py`). Kubeconfig filtering, egress policy and telemetry tools are unchanged. |
| SLO-driven fidelity | `sregym/chaos/slo.py` parses every wrk2 round (error ratio, socket timeouts, p99, delivered throughput) and judges it against an SLO calibrated on pre-fault rounds. During its session the chaos agent can read the live verdict from `GET /chaos/slo`, which exists only for canvas problems. |
| Protected components | The observability namespace and the workload generator are fingerprinted (`sregym/chaos/protected.py`) before and after injection. Agents cannot see the generator at all (`agent_visibility_policy.py`). |
| Fault artifact | `inject.sh`, `recover.sh`, `mitigate.sh` (reference mitigation), `fault.json` (layer, primitive, root-cause class, incident class) and `design.md`. See `sregym/chaos/artifact.py`. |
| Hidden injector | Accepted artifacts replay as `gen_<id>` through `GeneratedProblem`. The conductor runs the scripts with an admin kubeconfig, never inside an agent session, so shell history and edited files never reach the fixer. A lint pass rejects scripts that would plant telltale names (`chaos`, `fault`, `inject`, ...) in cluster state. |
| Mitigation only | Generated problems have no diagnosis oracle. They are graded by `SLOMitigationOracle`, which requires every wrk2 round in a sustained 180 s window (after a 30 s buffer) to meet the SLO and protected components to be intact. No LLM judge is involved in acceptance or scoring. Fixers receive a mitigation-only instruction (`clients/harness/stages.py`). |

## Validity gates

All gates run in fresh DinD environments, with `--replays` fresh deployments (default 2):

* **Impact**: after injection, most wrk2 rounds over 150 s, including the latest, violate the SLO.
* **Observability**: at least one new Prometheus alert fires in the app namespace (alerts already firing at baseline are excluded).
* **Persistence**: the SLO is still violated after `--persistence` seconds (default 300) with no intervention.
* **Integrity**: protected components are unchanged and the workload generator is still running.
* **Reproducibility**: every replay passes the gates above.
* **Solvability**: the scripted `refmit` agent applies `mitigate.sh`, and the SLO oracle passes.
* **Realism**: `fault.json` maps the fault to a fault primitive and a real incident class. Realism still needs sampled human review; there is deliberately no LLM realism score.

Every fixer attempt also records the injection gates. An attempt whose fault was not live is discarded and retried.

## Difficulty filter

Each panel agent (`codex`, plus `claudecode` when `--claude-env-file` is given) gets `--k` attempts
(default 2). A candidate is rejected as soon as more than `--max-solves` attempts (default 1) succeed.
Candidates that stay within the band are accepted and copied to
`sregym/conductor/problems/generated/<id>/`, with the panel outcome recorded under
`fault.json:generation`. Previously generated root-cause classes and all existing problem ids are passed
to later chaos sessions to avoid duplicates. Sessions rotate across applications and themes.

## Running

```bash
python3 docker/dind/run.py build
# Optional Docker Hub pull-through cache shared by all DinD environments:
docker run -d --restart=always --name hub-mirror -p 5000:5000 \
  -e REGISTRY_PROXY_REMOTEURL=https://registry-1.docker.io registry:2
sudo HOME=$HOME python3 sregym/chaos/pipeline.py --work /mnt/docker/chaos --slots 3 \
  [--claude-env-file ~/claude.env]   # CLAUDE_CODE_OAUTH_TOKEN=..., adds Claude to the panel
```

State is kept in `<work>/ledger.json`; restarting resumes from there. `touch <work>/STOP` stops new
chaos sessions while letting in-flight candidates finish. Each environment uses `--cpus`/`--memory`
(default 5 CPUs / 18 GiB), so plan about 6 CPUs and 20 GiB of host capacity per slot.

Run an accepted problem like any other: `python main.py --problem gen_<id> --agent codex --model ...`.

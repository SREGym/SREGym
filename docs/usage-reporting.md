# Usage reporting

`usage_reporting` runs an external QEMU company world through the normal
SREGym conductor, diagnosis submission, mitigation submission and oracles.
The customer opening and `companyctl` reference are the public problem assets.

## External bundle

Set `SREGYM_USAGE_WORLD_ROOT` to an installed bundle with this layout.

```text
$SREGYM_USAGE_WORLD_ROOT/
  build/
    revive.sh
    companyctl
    lab.py, revive.py, world_policy.py, world_run_guard.py, world_guard.py
    world-config.json
    public-instances.json
    artifacts/
    vm/
      states/<state>/ready.json
  grading/
    grade.py
    diagnosis/<state>.json
    results/
```

The bundle supplies the runtime modules, disk backing closure, native saved
state, private grading inputs and their dependencies. `revive.sh STATE RUN_DIR`
creates a fresh run and `prepared.json` with its saved-live guest list. The
runtime modules retain their QMP, process ownership, resource watcher and
capture interfaces. `world-config.json` records `probe_run` and
`artifact_subdir`; the watcher writes `resource-current.json` there.

| Variable | Default |
| --- | --- |
| `SREGYM_USAGE_STATE` | `P6` |
| `SREGYM_USAGE_STATES_DIR` | `$SREGYM_USAGE_WORLD_ROOT/build/vm/states` |
| `SREGYM_USAGE_GRADER` | `$SREGYM_USAGE_WORLD_ROOT/grading/grade.py` |
| `SREGYM_USAGE_DIAGNOSIS_KEY` | `diagnosis/<state>.json` beside the grader |
| `SREGYM_USAGE_RUNS_DIR` | `~/.local/state/sregym/usage-worlds` |
| `SREGYM_RESULTS_DIR` | `~/.local/state/sregym/results` for this problem |

Use absolute paths for overrides and neutral output directories. Bundle paths and
grading inputs are never mounted into the agent container.

## Run

The host needs Linux x86_64, accessible `/dev/kvm`, QEMU, systemd, SSH, gzip and
Docker. Preflight refuses an occupied bundle before changing any services.
Only one run may use a bundle's global state and ports at a time.

```sh
export SREGYM_USAGE_WORLD_ROOT=/path/to/company-world
python main.py --problem usage_reporting --agent claudecode --model claude-opus-5-5 \
  --reasoning-effort xhigh --judge-backend codex --judge-model "$JUDGE_MODEL" \
  --stages diagnosis mitigation --agent-timeout 1800 \
  --internet-access filtered --container-hardening on
```

After revive and readiness checks, the resource watcher starts and T0 is
recorded. One 1800-second budget includes agent startup, diagnosis judging and
mitigation. At T0+1800, an independent helper stops the agent, freezes company
operation processes and pauses every running guest. It exports native RAM and
device state, retains immutable disks in `end-state/`, runs the mitigation
grader and tears down owned services. Early submission waits for this cutoff;
a missing submission remains incomplete even if teardown grades the end state.

The agent gets public tools, its own output and the selected model transport.
The existing Docker egress proxy permits the documented company endpoints and
conductor routes through a fixed HTTP gateway. Kubernetes, host source and host
home mounts are omitted. Direct host bridge access is disabled. Codex session
files persist directly in the attempt output so the deadline kill retains them.

## Grading

Diagnosis uses the standard `LLMAsAJudgeOracle` and `DiagnosisJudge`, with the
standard checklist and its 0.70 threshold. The `composite_score` ranges from
0 to 1; scores of 0.70 or above pass. The external JSON key supplies
`schema: usage-diagnosis-v1`, the selected `saved_state`, and one `root_cause`
with `component` and `description` strings. The problem formats that description
with `build_structured_root_cause`, like other SREGym problems. Missing or
mismatched truth fails before deployment. No custom checklist or per-layer
judging is used.

The mitigation oracle calls the configured grader with `RUN_DIR --capture
END_STATE --output OUTPUT --runtime-limit 1800` within 1860 seconds. It requires
`GRADE.json` with schema `sregym-grade-v1`, a matching `run`, and
`mitigation.status` equal to `pass` or `fail`, mapped to a boolean `success`.
Incomplete or invalid grader outcomes, and runs the harness cut short (for
example at a host resource limit), are harness failures and are not graded. Full
tables, limitations and validity metadata remain in `GRADE.json`.

Mitigation passes only when all of these hold at the deadline:

- **Custody.** Every required reading (each one that still had a copy at handoff,
  and each one accepted during the run) survives, either in its regional ledger or
  in a preserved copy. None is destroyed, and none appears more than once in a
  ledger.
- **Freshness.** Every reading accepted during the run that is due by the deadline
  (offered at least 120 seconds before it) is in its regional ledger. Inside the
  first 600 seconds, a restoration window, readings may arrive late but never go
  missing; after it, they arrive within 120 seconds.
- **Service.** Both customer endpoints serve, and every fleet device has a ledger
  reading within 120 seconds of the deadline. The world watcher probes each
  `/health` and `/api/live` endpoint six seconds before the freeze and writes
  `service-health-at-D.json`; an endpoint that has not answered by the deadline
  counts as not serving. Fleet devices are those the regional device clients have
  acknowledged, whether or not they are online.
- **Recurrence.** Re-running the scheduled role, profile and package maintenance on
  a private copy of the end state, with automation restored, removes no routing
  rule and breaks no connectivity on any host.

## Resources and distribution

The default capsule allocates about 20.5 GiB across eight company guests and an
ops helper, with 18 guest vCPUs sharing a 10-CPU slice. It has been run on a
12-vCPU host with 48 GiB RAM. A run grows the world's disks by up to about 14 GiB
when the agent copies rescued data, and keeps about 7 GiB of end-state capture and
4 GiB of grader output; start each run with at least 30 GiB free. The bundle's
resource watcher keeps its configured memory cap, CPU quota, swap, host RAM and
disk checks; guest failures remain part of the task.

Distribute a versioned external archive with checksums, the complete QCOW2
backing closure and gzip native QEMU RAM/device streams. The default capsule is
about 22 GiB before tools and grading inputs, with 168 GiB virtual disk capacity.
Normalize absolute paths and pin compatible QEMU, machine and CPU features
before release. Saved images and grading material are not part of this PR.

## Tests

```sh
python -m pytest -q tests/usage_reporting
```

These tests use fake capsules, mocked QMP/process commands, loopback HTTP
fixtures and the normal conductor API. They do not start a company world.

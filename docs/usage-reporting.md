# Usage reporting

`usage_reporting` is an SREGym 2.0 problem inspired by Datadog's March 2023 outage. It runs on Smart Meter, a
two-region IoT telemetry company on QEMU hosts. Per region, Smart Meter has:
- a Kubernetes cluster with Cilium;
- EMQX, Kafka and a PostgreSQL ledger;
- usage reporting and partner statements.

The fleet-wide platform repository and automation sit on top of that, along with 2,000 device clients.

The problem goes through the normal SREGym conductor: a diagnosis submission, a mitigation submission and
oracles. The agent gets the customer opening and the company tools reference, both public problem assets, and works
through `companyctl` and the company's own endpoints.

- Application adapter: `sregym/service/apps/smart_meter/` (`SmartMeter`, registered as `smart-meter`).
- Application source: `SREGym-applications/smart-meter` (world runtime, scenario build, grader, pinned inputs). The
  adapter readies, freezes and tears down the world through `world/sregym_lifecycle.py` there.

## Install the world

Smart Meter revives a saved world: every host resumes from its saved RAM and disks exactly where the world was saved.

The saved world is one package (10.2 GB) in the Hugging Face dataset
[`munimthahmid/usage-reporting-world`](https://huggingface.co/datasets/munimthahmid/usage-reporting-world).
The first run downloads it, checks its sha256, installs it and deletes the download. Later runs reuse the installed
world. To install it ahead of time instead:

```sh
hf download munimthahmid/usage-reporting-world smart-meter-usage-reporting.tar.zst --repo-type dataset --local-dir ~/usage-reporting-world
cd SREGym-applications/smart-meter
python3 world/bundle.py install ~/usage-reporting-world/smart-meter-usage-reporting.tar.zst
```

`bundle.py install` unpacks the saved world into `world/vm/states/usage-reporting` (and the guests' SSH key, boot seeds
and the Ubuntu base image into `world/`), checks every file against the package's checksums and points the saved disks
at this folder. A run needs nothing else. To build a world from source instead, see the Smart Meter README (about 4 hours,
and it fetches its own pinned inputs).

The problem uses the world in `smart-meter` in SREGym's applications checkout, saved state `usage-reporting`, and keeps each
run's records in `~/.local/state/sregym/usage-worlds`. World files and grading inputs are never mounted into the agent
container.

## Run

**Host requirements:**
- any x86-64 Linux machine with KVM (`/dev/kvm`): bare metal, or a cloud VM with nested virtualization. The saved
  guests use QEMU's generic `Westmere-v2` processor model, so any Intel processor from 2010 on with AES-NI can wake
  them. AMD hosts are untested;
- QEMU 8.2 or newer, a systemd user session, SSH, curl, gzip, zstd and Docker;
- SREGym's own requirements from the README, including kubectl and Helm: SREGym checks for both before every
  problem, even this one, which uses no Kubernetes cluster;
- passwordless `sudo`, because the grader reads frozen disks in a private mount namespace;
- 32 GB of memory and 12 CPUs (tested on 48 GB and 12 CPUs). The world's guests use about 21 GiB at their peak, inside
  a 10-CPU, 28 GiB slice;
- disk: about 60 GB free for the 10.2 GB download, the 18 GB installed world and 30 GiB for each run.

`bash SREGym-applications/smart-meter/setup_host.sh` installs the packages on apt-based systems, adds you to the `kvm` and
`docker` groups and checks that the host can wake the saved guests (processor model, machine type, memory).

Only one run per installed world may be active at a time, and preflight refuses an occupied world.

```sh
python main.py --problem usage_reporting --agent claudecode --model claude-opus-5-5
```

The defaults apply as for any problem: 1800 s agent timeout, filtered internet access, container hardening on,
the default judge.

If a run is interrupted (for example SREGym itself is killed), the world, the agent container and the egress proxy
stay up. Tear them down with:

```sh
cd SREGym-applications/smart-meter
python3 world/sregym_lifecycle.py teardown --project . --run "$(ls -td ~/.local/state/sregym/usage-worlds/run-* | head -1)"
docker rm -f $(docker ps -aq --filter name=sregym- --filter name=evaluation-egress)
docker network rm $(docker network ls -q --filter name=evaluation-egress)
```

**Run lifecycle:**
- Deploy installs the world if it is missing (first run), revives it, checks that it is ready and starts the host
  resource watcher.
- SREGym owns the run length (`--agent-timeout`) and stops the agent, as for any problem.
- When the agent submits mitigation, the mitigation oracle:
  - probes the customer endpoints and reads the guest clock;
  - stops company operation processes and pauses every guest;
  - exports RAM and device state and keeps the disks in `end-state/`;
  - runs the mitigation grader.
- The application's cleanup tears the world down. A run that never submits mitigation is not graded.

**What the agent can reach:**
- The problem declares the seven company endpoints on its tools page (`agent_endpoints`). The world serves them on
  host loopback and on the Docker bridge gateway address, which agent containers reach as `host.docker.internal`.
- In filtered mode, the egress proxy allows those endpoints, the conductor routes and the selected model transport.
- The agent downloads `companyctl` from the company operations API.

## Grading

**Diagnosis** uses the standard `LLMAsAJudgeOracle` and `DiagnosisJudge`, with the standard checklist and its
0.70 threshold.
- The key is a JSON file containing `schema: usage-diagnosis-v1`, the selected `saved_state`, and one `root_cause`
  with `component` and `description`.
- The problem formats it with `build_structured_root_cause`, like other SREGym problems.
- A missing or mismatched key fails before deployment.
- Keys stay in SREGym, outside the application. Only `OPENING.md` and `COMPANY-TOOLS.md` reach the agent.

**Mitigation** is graded from the end state at submission only. No check depends on when during the run
something happened.
- The oracle calls the grader with `RUN_DIR --capture END_STATE --output OUTPUT --runtime-limit SECONDS`.
- It requires a `GRADE.json` with schema `sregym-grade-v1`, a matching `run`, and `mitigation.status` of `pass` or
  `fail`.
- An invalid grader outcome, or a run the harness cut short (for example at a host resource limit), is a harness
  failure and is not graded.

The pass rules are in `SREGym-applications/smart-meter/grader/RUBRIC.md`. Mitigation passes only when all six checks pass:

1. **Custody.** Every required reading must be in its own region's ledger exactly once. A copy in the other region's
   ledger counts as an extra copy, and a copy kept anywhere else does not count, because usage totals come from the
   ledgers. Required readings are:
   - readings acknowledged and still somewhere at handoff;
   - readings accepted during the run, except those accepted in the last 120 s before submission.
2. **Freshness.** Every reading a device offered during the run at least 120 s before submission is in its regional
   ledger at submission, whether or not the broker acknowledged it.
3. **Service health.** Both customer endpoints serve at submission, and every fleet device (every device the
   regional clients have acknowledged, online or not) has a reading in its regional ledger at most 220 s old at
   submission (devices report every 100 s, so this is one reporting interval plus the 120 s freshness budget).
4. **Recurrence.** On a private fork of the end state with automation restored, every host starts with Cilium's
   routing policy rules, and routine package and role runs on every host remove none of them and break no
   connectivity. The role run counts only when it clones the platform repository again, so the fix has to be in
   the repository.
5. **Reporting.** Settled reports and both regions' partner statements match the final ledgers, at submission and after
   150 s of routine operations on the fork.
6. **Safety.** No only-copy source (stopped or held host disks, orphaned gateway sessions) has a proven loss.

Full tables, limitations and validity metadata are in `GRADE.json`.

## Tests

```sh
python -m pytest -q tests/usage_reporting
```

The tests use fake saved worlds, mocked world commands and the normal conductor API. They do not start a company
world. The lifecycle helper's own tests (mocked QMP and process families, loopback HTTP fixtures) live with it:

```sh
cd SREGym-applications/smart-meter/world && python3 -m pytest -q test_sregym_lifecycle.py
```

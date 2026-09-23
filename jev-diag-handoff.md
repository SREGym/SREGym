# jev_diag handoff

Status as of 2026-09-22.

## Code map

| File | Role |
|---|---|
| `driver.py` | Entry point: conductor handshake, start delay, collection, diagnosis, submission, artifacts |
| `config.py` | `--model` handling for `main.py`, and the `jev_model()` lookup used inside the container |
| `collector.py` | All kubectl reads and summaries: workloads, pods, events, Services, NetworkPolicies, quotas, webhooks, CoreDNS, RBAC roles, config objects, logs after warm-up, token budgets |
| `derive.py` | Log classes, cross-component error pointers with backend role synonyms, telemetry-noise classification, change ranking, evidence kinds, cluster findings |
| `classifier.py` | Triage questions, cause categories, object kinds, the one-shot diagnoser, fault-object selection, text assembly |
| `tree.py` | The iterative decision tree: queue, investigate node, cluster node, stopping and fallback rules |
| `investigate.py` | Per-component detail: full spec, events, logs split around the latest change, ConfigMap data, spec checks, RBAC, dependency state |
| `trace.py` | Decision trace and Stratus trajectory output |
| `replay.py` | Offline triage replay of saved snapshots |

Integration outside the package:

- `agents.yaml`: the `jev_diag` entry.
- `main.py`: the preflight map entry, `configure_jev_diag`, and `DEFAULT_AGENT_MODEL`.
- `sregym/service/container_runner.py`: forwards `TYPESAFE_API_KEY` only while `AGENT_JEV_MODEL` is
  set. This gate is shared with the Codex decision tool.
- `pyproject.toml` and `docker/agents/requirements-container.txt`: `typesafe-sdk`.
- `tests/clients/test_jev_diag.py`: 20 tests covering collector helpers, budget fitting, and the
  one-shot classifier. Nothing covers `derive.py`'s newer rules, `investigate.py`, or `tree.py`.

## Design (tree v3)

1. Collect the cluster state with kubectl and derive signals in code.
2. Triage: one Jev request ranks every component plus `other`, which stands for cluster-level objects.
3. Queue: every triage candidate with probability of at least 0.05, up to 6. Up to 4 more are admitted
   on evidence alone, strongest first: configuration, pointed at by errors, own failure logs, change,
   symptoms. Telemetry components without symptoms of their own go last.
4. Investigate the front candidate: fetch its detail, then ask one Jev request with four questions.
   These are the verdict (origin, victim, unrelated, undetermined), the next component, the cause
   category, and the key evidence item.
5. Transitions. Origin at 0.6 or above concludes, unless triage ranked an unexamined candidate higher.
   Origin at 0.9 or above concludes regardless. Victim at 0.5 or above, with a next-component answer at
   0.3 or above, moves that dependency to the front. The dependencies named in its error lines follow
   right behind it.
6. `other` goes to a cluster node that chooses among findings: quotas, webhooks with dead backends,
   CoreDNS changes, NetworkPolicies, and Roles or ClusterRoles bound to the workloads.
7. The budget is 18 steps. The fallback is the best origin probability of at least 0.4, then `other`
   if triage chose it and findings exist, then the triage top choice.
8. The text is assembled from the answers and collected facts. Every mismatch code found in the chosen
   component's spec is listed, because a fault can have more than one mechanism.

Moving from v1 (6/21) to v3 (14/21) changed the evidence far more than the questions:

- error lines are split before and after the latest application change;
- telemetry export errors are classified as noise;
- code checks probe ports against container ports, and env addresses against Service ports;
- dependencies are judged from their own pods, not from the candidate's complaints;
- RBAC roles can be named as the fault object;
- the agent waits for the fault to surface before reading the cluster.

## Results

| Design | Judged correct | Mean composite | Jev requests per problem |
|---|---|---|---|
| One-shot, local kind cluster | 6/21 | n/a | n/a |
| One-shot, CloudLab | 9/21 | 0.47 | 1.9 |
| Tree v1 | 6/21 | 0.36 | 3.8 |
| Tree v2 | 11/21 | 0.57 | 3.5 |
| Tree v3, current | 14/21 | 0.58 | 3.7 |

- The judge was Claude Sonnet 5 at medium effort, with a pass threshold of 0.70 on the composite of
  localization, characterization, and scope.
- Each problem had one attempt. All 21 problems were used during development, so these are
  known-problem results, not a held-out evaluation.
- In v3 the agent worked 18.4 s per problem on average, plus the 120 s wait. The snapshot took 11.6 s,
  mostly log fetches. The tree took 6.7 s, of which 1.6 s was Jev latency.
- A harness cycle takes about 10 minutes per problem. The full suite took 51 minutes sharded across
  the five clusters.

Run directories are under `results/remote-tree/<orchestrator>/results/`, named by the remote clock,
which runs about 12 minutes behind the local machine:

- v1: `0921_2035` to `0921_2109`
- smoke tests: `0921_2116`
- v2: `0921_2126` to `0921_2256`
- v3: `0921_2257` onward

The one-shot CloudLab runs are under `results/remote/`, and the kind run is in `results/0917_2216/`.
Shard logs sit beside the results in `jev-eval/`, `jev-eval-v2full/`, and `jev-eval-v3/`.

The per-problem comparison of every run is in `results/remote-tree/comparison_table.txt`, with
totals in `results/remote-tree/comparison_summary.txt`.

## v3 failures and next steps

| Problem | Why it fails | Proposed fix |
|---|---|---|
| `valkey_auth_disruption` | The password changes inside the process. The server looks healthy, and the client log lacks the NOAUTH detail. | Add an active probe: when a victim's named dependency looks healthy, exec a connection test from the victim's pod. |
| `internal_traffic_policy_local_astronomy_shop` | Passed in v2. With 18 steps, a late weak candidate at 0.45 won the fallback. | Weigh the fallback by triage probability. |
| `service_dns_resolution_failure_social_network` | Same fallback problem. The CoreDNS finding is not linked to the service whose name fails. | Same fix, plus link DNS findings to the affected service. |
| `mutating_webhook_resource_limits_social_network` | The webhook rewrote pod memory at admission. The Deployment spec is unchanged. | Compare live pod resources with the workload template. |
| `secret_rotation_stale_env_credentials_astronomy_shop` | Picks the database with the auth failures. The judge wants the client holding stale credentials. The v3 judge output also failed to parse. | Rule: a consumer whose Secret changed after its pods started is the origin. |
| `kafka_poison_pill_hol_block` | The ground truth is a Kafka record, not a Kubernetes object. | Describe an unprocessable record on the topic and name the producer. |
| `search_rate_retry_collapse_hotel_reservation` | The retry storm shows in RPC latency, which is never collected. | Read latency metrics. |

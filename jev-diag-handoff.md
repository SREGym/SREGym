# jev_diag handoff

Status as of 2026-09-25.

## Code map

| File | Role |
|---|---|
| `driver.py` | Entry point: conductor handshake, start delay, trend sample, collection, exec probes, diagnosis, submission, artifacts (including the raw kubectl bundle) |
| `config.py` | `--model` handling for `main.py`, and the `jev_model()` lookup used inside the container |
| `collector.py` | kubectl reads and summaries: workloads, pods, events, Services, NetworkPolicies, quotas, webhooks, CoreDNS rules, RBAC, config objects, logs with per-signature recency, token budgets; raw recording and replay |
| `changes.py` | Change detection from each object's own history (managedFields, pod-template revisions), symptom-onset estimate, change ranking |
| `checks.py` | Cross-object checks: pod vs template and matching webhooks, Local traffic policy vs client nodes, Secret/ConfigMap read modes, stuck finalizers and missing permissions, shared ReadWriteOnce claims |
| `derive.py` | Log classes (severity parsing, gRPC and socket codes), error pointers from still-occurring errors, server login rejections and candidate clients, evidence kinds, cluster findings |
| `investigate.py` | Per-component detail: spec, current events, logs, repeated failures, spec and Service checks, RBAC, dependency state, input candidates |
| `tree.py` | Decision tree: queue, investigate node, cluster node, other-side check with pairwise question, fallback, fault-object choice, victim-chain characterization |
| `classifier.py` | Triage questions, cause categories, one-shot diagnoser, fault-object selection with linkage, text assembly |
| `probes.py` | Opt-in read-only exec probes (backend health command, broker consumer progress), gated on container memory headroom |
| `trend.py` | Two light samples an interval apart; growth is reported |
| `trace.py`, `replay.py` | Decision trace and trajectory; offline replay of snapshots or raw bundles |

Integration outside the package: `agents.yaml` (the `jev_diag` entry), `main.py` (preflight map,
`configure_jev_diag`), `sregym/service/container_runner.py` (forwards `TYPESAFE_API_KEY` only while
`AGENT_JEV_MODEL` is set), `typesafe-sdk` in the requirements. `sregym/conductor/oracles/llm_as_a_judge/judge.py`
also gained a tolerant checklist parser (see Results); drop it if it should not ship with this branch.

## Design (v4.2)

1. Collect with kubectl. A light pod/event sample is taken 45 s before the 120 s start delay ends, and
   growth between the two samples is reported. Every kubectl call is recorded for offline replay.
2. Changes are judged against each object's own history, never an assumed deploy time: a spec write after
   creation, an object created after the workloads it acts on, or a pod-template diff between revisions.
   They are ranked by time relative to the first still-active symptom.
3. Error signatures are ongoing or stopped by their own rhythm; only ongoing errors link components.
4. Code checks: live pod vs its template (admission rewrites; requests defaulted from limits are not
   differences), Local traffic policy vs client nodes, how each Secret or ConfigMap is read (env values are
   fixed at container start), CoreDNS rules naming an app Service, stuck finalizers, shared RWO claims,
   duplicate env values, full probe targets.
5. Triage ranks components plus `other`; the cluster node is queued early when triage leans to cluster
   objects.
6. Investigate asks verdict, linked next hop, category, key evidence, and whether one input item blocks
   the component. With `--exec-probes`, a healthy-looking backend named by a victim gets a health command.
7. Origin ≥ 0.6 concludes, but for auth or permission causes the other side (top two candidate clients) is
   examined first, and a pairwise question settles two origins. Fallback: an `origin` verdict ≥ 0.4, then
   the end of a strong victim chain (explained from the victim's failure), then triage without telemetry.
8. The fault object comes from the cause category and must act on the concluded component; the text leads
   with it when it carries the fault.

## Results

All CloudLab runs share the harness; only the agent changed. Judge: Sonnet 5, medium, pass at 0.70.

| Run | Passed | Mean | Loc / Expl / Scope | Jev req avg |
|---|---|---|---|---|
| One-shot | 9/21 | 0.47 | | 1.9 |
| Tree v3 | 14/21 | 0.58 | 0.67 / 0.51 / 0.57 | 3.7 |
| v4 | 18/21 | 0.84 | 0.95 / 0.83 / 0.75 | 2.6 |
| v4.1 | 17/21 | 0.86 | 0.92 / 0.78 / 0.89 | 2.6 |
| v4.2 (probes on) | 18/21 | 0.89 | 0.98 / 0.81 / 0.89 | 2.5 |

- v4 had one judge parse failure (edge_request_filter, scored 0). The parser fix removed them in v4.1/v4.2.
- v4's admission check falsely flagged pods whose requests were defaulted from limits (23 of 25
  astronomy-shop components); it cost valkey_auth and two scope scores. Fixed in v4.1.
- v4.2 solved valkey_auth through the probe (`valkey-cli ping` → NOAUTH) and secret_rotation through the
  new credential ranking.
- **v4.2's broker probe ran a JVM tool inside the memory-capped Kafka broker and OOM-killed it.** That
  caused kafka_poison_pill's failure (0.33) and may have disturbed the other seven astronomy-shop runs after
  their snapshot. Since then every probe requires memory headroom (8 MiB native, 768 MiB JVM/Node); on the
  v4.2 data the Kafka tool would be skipped and `valkey-cli` still runs. Not yet evaluated.
- Single runs; judge and triage variance moves individual problems by 0.1 or flips borderline ones. All 21
  problems were used for development, so these are known-problem results.

Run directories under `results/remote-tree/<orchestrator>/results/` (remote clock ~12 min behind): v3
`0921_2257`–`0921_2359`, v4 `0924_*` before 22:00, v4.1 `0924_22*`–`0924_23*`, v4.2 `0925_*`. Shard logs are
in `jev-eval-v3/`, `jev-eval-v4/`, `jev-eval-v41/`, `jev-eval-v42/`; `results/remote-tree/compare_v3_v4.py`
(`--v41`, `--v42`) prints the per-problem comparison. On the orchestrators, probes were enabled through the
`jev_diag` entry's `kickoff_env` in `agents.yaml` (the repo default is off), and `judge.py` was patched in place.

## Open problems

| Problem | Status in v4.2 | Next step |
|---|---|---|
| `kafka_poison_pill_hol_block` | Broker crashed by the probe | Rerun with the headroom guard (v4.3) |
| `edge_request_filter_cpu_saturation` | Right component; blames the CPU limit change instead of the regex filter added in the same revision | Ask which change explains the CPU use when several fields changed together |
| `search_rate_retry_collapse_hotel_reservation` | Right component (rate); cites rate's QPS limit without search's retry settings | Examine the caller of a component whose clients time out, and describe both sides |
| All | One run each, development suite | Repeat runs; evaluate on problems outside SREGym-Lite |

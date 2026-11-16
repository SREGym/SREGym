# Handoff: benchmarking TracePress on agent failure attribution

Research started 2026-10-30; handoff written 2026-11-16. Nothing here is part of SREGym
proper: it lives on the branch `claude/inspiring-dijkstra-ymar8s` and is not meant to merge as is.

## The question

The user asked whether [TracePress](https://github.com/Ziems/tracepress), a library for
LLM-powered map/reduce over agent traces, could be benchmarked on
[Who&When](https://github.com/ag2ai/Agents_Failure_Attribution) to see if it performs
better than existing methods. Follow-ups asked about newer work in the space, whether a
TracePress-style harness could beat state-of-the-art methods, and which benchmarks are most
established.

"Failure attribution" here means: given a failed agent run, name the agent and the step
that caused the failure.

## Status

| Item | State |
|---|---|
| Read TracePress and Who&When code and data | Done |
| Harness that loads Who&When into TracePress and runs 3 pipelines (`harness/whowhen_tp.py run`) | Written, **never run** |
| Scorer with strict and paper-compatible metrics, CIs, paired differences (`whowhen_tp.py score`) | Written and tested |
| Runner for the paper's 3 baselines on any OpenAI-compatible endpoint (`harness/run_baselines.py`) | Written, never run |
| Cost model per method (`harness/estimate_cost.py`) | Written and run |
| Survey of newer work and of which benchmarks are established | Done (below) |
| Any real accuracy numbers for TracePress | **None yet** |

### Blockers

1. **Running TracePress was blocked.** In the original session, installing TracePress from
   its cloned repo (`uv pip install ./ext/tracepress`) was refused by Claude Code's
   auto-mode permission classifier as "code from external". The user has to allow it, or
   run the harness on their own machine.
2. **No LLM API key.** The container had no OpenRouter, OpenAI or Anthropic key. The
   harness defaults to OpenRouter. In a cloud session, use the `request_credential` tool for
   host `openrouter.ai` once the user agrees to the spend.
3. **Dataset terms unchecked.** TRAIL and AgentRx require accepting terms on Hugging Face
   before download. LongRCA's dataset page states no license. Who&When's Hugging Face page
   states none either (its GitHub repo is MIT).

### Decisions the user hasn't made yet

- Which backbone model to use for every method. It must be the same model for TracePress
  and the baselines.
- Budget. Rough guess: $1–5 per full Who&When pass on a cheap model.
- Whether results should live in SREGym, a fork of TracePress, or a separate repo.

## Repos and data

| What | Where | Version read |
|---|---|---|
| TracePress | https://github.com/Ziems/tracepress | `d4782da` (release 0.1.3, 2026-10-27) |
| Who&When code and data | https://github.com/ag2ai/Agents_Failure_Attribution | `f4d2b6d` (2026-02-05) |
| Who&When paper | https://arxiv.org/abs/2505.00212 (ICML 2025); also `paper.pdf` in that repo | — |

Clone both into a scratch directory; nothing from them is vendored here. Run Python that
reads the downloaded data with `-I`.

## What we learned

### TracePress, as it bears on this

- Rows carry a hierarchical key such as `(("trace", id), ("step", i))`. `tp.from_rows([tp.Row(key, values)])`
  makes a table from rows in memory.
- `lm_map`: one LM call per row. `group_by(...).lm_reduce(...)`: one call per group. Inputs
  naming a child column get an aligned list, inputs naming a group key get a scalar. Groups
  over `reduce_chunk_chars` (120K chars) are split and tree-merged.
- **A reduce sees only the named child columns, not the row key**, so the step number has to
  be a column (`step`) for the LM to cite it.
- `lm_fold`: carries state through a group's children, one sequential call per child. Fields
  on both sides of the arrow are state; the LM sees them as `previous_<name>`.
- No default LM: call `tp.configure(lm="openrouter:openai/...")`. Telemetry is on by default;
  the harness turns it off.
- LM operators default to `on_error="skip"`. A failed call silently drops that trace, so
  check `tp.last_run()` and the "wrote N of M predictions" line.
- There is no loop operator and no join (joins are listed as deferred in its `docs/SPEC.md`).
  Cross-table lookups are done with a Python dict inside `map`.
- It reads ATIF trajectories (`tp.read.atif`), and SREGym's `atif_converter/` writes ATIF, so
  the same pipelines could run on SREGym agent runs.

### Who&When data

- 184 failed runs. **Hand-Crafted**: 58 runs from Magentic-One, 5–130 steps (median 32.5),
  up to 330K chars. **Algorithm-Generated**: 126 runs from CaptainAgent, 5–10 steps.
- Each JSON has `history`, `question`, `ground_truth`, `mistake_agent`, `mistake_step` (a
  string holding the 0-based index into `history`) and `mistake_reason`. The speaker field
  is `role` in Hand-Crafted and `name` in Algorithm-Generated.
- Hand-Crafted roles carry suffixes (`Orchestrator (thought)`, `Orchestrator (-> WebSurfer)`)
  while labels say `Orchestrator`. After stripping the suffix, the labeled agent is the
  labeled step's speaker in 178 of 184 runs. The exceptions are Hand-Crafted 20, 22 and 49,
  plus 3 Algorithm-Generated runs.
- Algorithm-Generated `91.json` is labeled `Blu-Ray_Expert`. The upstream regex `[\w_]+`
  can't read the hyphen, so that run can't be scored correct for any method upstream. Our
  scorer allows hyphens.
- Upstream `Automated_FA/evaluate.py` checks **substring containment** for both agent and step,
  so a true step `1` matches a predicted `12`. On a random guesser for Hand-Crafted, that gives
  5.2% step accuracy against 1.7% exact. Our `paper_*` metrics reproduce upstream exactly;
  `agent` and `step` are exact matches.

### Published baselines (GPT-4o, from the paper's Table 1)

| Method | With ground truth: agent (algo / hand) | step (algo / hand) | Without: agent (algo / hand) | step (algo / hand) |
|---|---|---|---|---|
| Random | 29.10 / 12.00 | 19.06 / 4.16 | same | same |
| All-at-once | 54.33 / 55.17 | 12.50 / 5.26 | 51.12 / 53.44 | 13.53 / 3.51 |
| Step-by-step | 35.20 / 34.48 | 25.51 / 7.02 | 26.02 / 32.75 | 15.31 / 8.77 |
| Binary search | 44.13 / 51.72 | 23.98 / 6.90 | 30.11 / 36.21 | 16.59 / 6.90 |

The paper's finding: reading the whole trace is best at who, judging step by step is best at when.

### Trivial baselines are strong

Chosen by looking at the labels, so not fair methods, but any claimed gain has to clear them:

- Always `WebSurfer`: 56.9% agent accuracy on Hand-Crafted, above GPT-4o all-at-once (55.17).
- Always step 8: 15.5% step accuracy on Hand-Crafted, above every method in the paper.
- Always step 1: 27.0% step accuracy on Algorithm-Generated, above every method in the paper.
- Always `Verification_Expert`: 14.3% agent accuracy on Algorithm-Generated.
- A uniformly random step, attributed to its speaker (seed 0): Hand-Crafted 41.4% agent and
  1.7% step; Algorithm-Generated 31.7% agent and 13.5% step.

At about 30% accuracy, the 95% interval is roughly ±12 points for 58 runs, ±8 for 126,
±6 for 200 and ±3 for 1,140.

### Cost model (`estimate_cost.py`; prompt tokens ≈ chars / 4)

| Hand-Crafted, 58 runs | LM calls | Prompt tokens | Rounds that must run in order |
|---|---|---|---|
| All-at-once | 58 | 1.0M | 1 |
| Step-by-step | 937–2,993 | 7.2–42.6M | up to 130 |
| Binary search | 330 | 2.0M | 8 |
| TracePress map→reduce | 3,051 | 2.5M | 2 |
| TracePress fold | 2,993 | 3.4M | up to 130 |

Step-by-step re-sends the history at every step, so its cost grows with the square of trace
length. On Algorithm-Generated (at most 10 steps) the methods are within about 3× of each other.

### Newer work

All numbers are as the authors report them in their abstracts; full papers weren't read.
Citation counts are from Semantic Scholar on 2026-10-30; downloads are Hugging Face's
30-day counts on the same day.

| Benchmark | Venue, date | Citations | What it labels | Size | Notes |
|---|---|---|---|---|---|
| **Who&When** | ICML 2025 | 225 | Agent and step | 184 runs | De facto standard; 3.6K downloads. |
| **MAST / MAD** | NeurIPS 2025 | 806 | Failure type (14 modes) | 7 frameworks | Classifies failures; doesn't localize them. HF `mcemri/MAST-Data`, CC-BY-4.0. |
| **TRAIL** (Patronus) | arXiv, May 2025 | 116 | Error location and type | 148 runs, median ~100K tokens | Established long-trace benchmark. HF `PatronusAI/TRAIL`, MIT, terms required. |
| AgentErrorBench | arXiv, Sep 2025 | 156 | Error step and type | ALFWorld, GAIA, WebShop | No official dataset copy found. |
| TracerTraj (AgenTracer) | arXiv, Sep 2025 | 139 | Who and when | — | Mostly used as training data. |
| AgentRx (Microsoft) | arXiv, Feb 2026 | 72 | Critical step and category | 170 runs, 11 settings, incl. incident management | HF `microsoft/AgentRx`, CC-BY-4.0, terms required. Closest to SREGym. |
| TraceElephant | ACL 2026 | 37 | Who and when, full traces | Re-runnable environments | Full traces improve accuracy by up to 76%. HF `TraceElephant/TraceElephant`. |
| MP-Bench | arXiv, Mar 2026 | 27 | Several plausible causes | — | Argues single-cause labels drive the "LLMs are bad at this" results. |
| Who&When Pro | arXiv, Jul 2026 | 19 | Who, when, type | 12,326 runs, failures injected | HF `Leoxx/whowhen_pro` is tagged with the paper; **not confirmed official**. |
| LongRCA Bench | arXiv, Aug 2026 | 1 | Role and root step | 1,140 runs, median 145 steps | HF `CLoud5-real/longrca-bench`, linked from its [project page](https://longrca-bench.github.io/); no license stated. LongRCA-Mini has 200 runs. Best baseline 13.2% exact step; their RCTA 24.1% (51.1% role). |
| CatchBench | arXiv, Aug 2026 | 6 | Catch before, during or after a run | 1,162 runs | Shows how label-generation shortcuts inflate scores. |
| MegaRCA-Mix | arXiv, Sep 2026 | 0 | Root cause | 50 runs, median 286K tokens (from a search summary, not the abstract) | Continual Search raises F1 from 0.471 to 0.608. |

Methods that report Who&When results: AgentDebugX (open-source toolkit; 28.8% exact
agent-and-step with qwen3.5-9b vs 21.7% for the best single-pass baseline), FAMAS
(spectrum analysis over replayed runs), AgenTracer (an 8B model trained with RL), DoVer
(tests a suspected cause by editing the run there and re-running).

The Continual Search paper, from September 2026, used **TRAIL, TELBench, AgentRx and
Who&When** as its existing benchmarks. That's a reasonable proxy for the current standard set.

Broader reliability, beyond attribution: τ-bench (1,635 citations) and τ²-bench (689) are
established, with pass^k (all k runs succeed) as the consistency metric. Also
[Towards a Science of AI Agent Reliability](https://arxiv.org/abs/2602.16666) (ICML 2026) and
[ReliabilityBench](https://arxiv.org/abs/2601.06112).

Not checked: TELBench, RootSE, HORIZON, "Failure as a Process", TrajDebug, OpenRCA.

## The harness

See `harness/README.md` for run commands. Summary:

- `whowhen_tp.py run --method all_at_once|map_reduce|fold` builds `trace > step` rows
  (columns `question`, `step`, `agent`, `content`, plus optional `ground_truth`) and writes
  predictions in upstream `evaluate.py`'s text format.
  - `all_at_once`: `group_by(trace).lm_reduce` over raw steps.
  - `map_reduce`: `lm_map` judges each step alone, then `lm_reduce` picks the decisive step from the notes.
  - `fold`: `lm_fold` with a running note plus the current suspect step and agent.
- `whowhen_tp.py score <files...>` scores any files in that format, including the baselines'
  outputs, with exact and paper-compatible metrics, bootstrap 95% CIs and paired differences.
  It was checked with `make_fake_preds.py`: the oracle scores 100%, and the `paper_*` metrics
  match upstream `evaluate.py` on a random file (41.38% / 5.17%).
- `run_baselines.py` passes an `openai.OpenAI` client to upstream `Automated_FA/Lib/utils.py`.
  Its methods only call `client.chat.completions.create`, so OpenRouter works without
  changing their prompts.

API assumptions in `run` that have never executed and need checking on the first run:
`tp.Row` construction, `group_by` with non-key columns (`question`, `ground_truth`) as extra
group keys, the fold signature with untyped state on the input side and typed outputs, and
`int` outputs parsing. Start with `--limit 5`.

## Recommended next steps

1. **Unblock.** Get permission to install and run TracePress, and an OpenRouter key via
   `request_credential`, or have the user run the harness locally. Settle the backbone model.
2. **Smoke test** each pipeline with `--limit 5`, fix what breaks, then run full Who&When for
   all 3 TracePress pipelines and the 3 baselines on the same model, with and without ground truth.
3. **Check availability** of TRAIL, LongRCA-Mini and AgentRx data, and of baseline code for
   AgentDebugX and RCTA, including terms and licenses.
4. **Add datasets**: TRAIL (established, long traces), LongRCA-Mini (newer, long, has a
   leaderboard), optionally AgentRx's incident-management runs. Each needs a loader into
   `trace > step` rows and a scorer for its own labels; TRAIL labels error spans, not single steps.
5. **Build the staged pipeline**, the design most likely to compete:
   1. Story: `lm_reduce` over the raw trace gives the plan, final answer and visible failure.
   2. Screen: `lm_map` over every step, given the story and the previous few raw steps; aim to miss nothing.
   3. Trace back: for each flagged step, ask whether it's a symptom of an earlier step.
   4. Decide: `lm_reduce` over candidates picks the earliest decisive step; take the agent from that step's speaker.
   5. Vote: repeat 2–4 several times or with several models and take the majority.
   6. Tune: DSPy prompt optimizers (GEPA, MIPRO) on a training set disjoint from every test
      set, e.g. Who&When Pro. TracePress doesn't expose its modules for this yet.
6. **Report accuracy against cost**, with paired CIs, on one backbone. Compare only against
   baselines re-run on that backbone, never against published numbers from other models.

Expectations: the structural advantage is on long traces (TRAIL, LongRCA), where map/reduce
keeps prompts bounded and parallel. On Who&When the edge is small and wins will mostly be
within noise. Log-only analysis can't match methods that re-run the agent (DoVer, FAMAS,
AgenTracer), or recover inputs the trace never logged (TraceElephant).

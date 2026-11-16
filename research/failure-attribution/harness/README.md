# TracePress on Who&When

A harness that runs TracePress pipelines on the Who&When failure-attribution benchmark and
scores them against the paper's baselines on the same model, with the same metrics.

| File | What it does | Tested? |
|---|---|---|
| `whowhen_tp.py run` | Loads Who&When as `trace > step` rows and runs one of three TracePress pipelines | **No** (running TracePress was blocked in the session where this was written) |
| `whowhen_tp.py score` | Strict and paper-compatible metrics, bootstrap CIs, paired differences between methods | Yes: oracle gives 100%, and the `paper_*` metrics match upstream `evaluate.py` exactly on a random file |
| `run_baselines.py` | The paper's all-at-once / step-by-step / binary-search, prompts unchanged, on any OpenAI-compatible endpoint | No (needs an API key) |
| `estimate_cost.py` | Prompt tokens and sequential depth per method, from the data alone | Yes |
| `make_fake_preds.py` | Oracle and random prediction files, for checking the scorer | Yes |

## Methods

| Method | TracePress shape | Paper analogue |
|---|---|---|
| `all_at_once` | `group_by(trace).lm_reduce(raw steps)`, tree-merged above 120K chars | All-at-once |
| `map_reduce` | `lm_map` judges each step alone, in parallel, then `lm_reduce` picks the decisive step from the notes | New: a per-step view and a whole-trace view |
| `fold` | `group_by(trace).lm_fold`, with a bounded running note and the current suspect step | Step-by-step, without re-sending the history |

The paper finds that all-at-once is best at **who** and step-by-step is best at **when**.
`map_reduce` is the one to watch, because it might get both.

## Run it

```bash
pip install tracepress openai
git clone https://github.com/ag2ai/Agents_Failure_Attribution
export OPENROUTER_API_KEY=...
D=Agents_Failure_Attribution/Who\&When

# smoke test first: 5 traces
python whowhen_tp.py run --data "$D" --subset hand --method map_reduce --lm openrouter:openai/gpt-6-luna --limit 5

for m in all_at_once map_reduce fold; do
  python whowhen_tp.py run --data "$D" --subset hand --method $m --lm openrouter:openai/gpt-6-luna
done
for m in all_at_once step_by_step binary_search; do
  python run_baselines.py --afa Agents_Failure_Attribution --subset hand --method $m --model openai/gpt-6-luna
done
python whowhen_tp.py score --data "$D" --subset hand outputs/all_at_once_openai_gpt-6-luna_hand.txt outputs/tp_*_hand.txt
```

Repeat with `--subset algo`, and add `--no-gt` for the setting where the ground-truth answer is hidden.

## Metrics

- `agent`, `step`: exact match. The agent name is normalized, so `Orchestrator (thought)` counts as `Orchestrator`.
- `agent_from_step`: the speaker of the predicted step. The labeled agent is the labeled step's speaker on 178 of 184 traces, so this rewards getting the step right.
- `step_pmK`: within K steps (the paper's Table 2).
- `paper_agent`, `paper_step`: upstream `evaluate.py`'s substring check, for comparison with published numbers. It is lenient: a true step `1` matches a predicted `12`. On a random guesser for Hand-Crafted it gives 5.2% step accuracy against 1.7% exact.

## Caveats

- **Trivial baselines are strong.** Always answering `WebSurfer` scores 56.9% agent accuracy on Hand-Crafted, above the paper's GPT-4o all-at-once (55.17%). Always answering step 1 scores 27.0% step accuracy on Algorithm-Generated, above every method in the paper (best: 25.51%). These were picked by looking at the labels, so they aren't fair methods, but any gain has to clear them.
- **Small n.** On 58 traces, one trace is 1.7 points. Use the paired CIs from `score`.
- **Same model for everything.** The paper's numbers are GPT-4o; compare only against `run_baselines.py` on your model.
- **Contamination.** The dataset has been public since May 2025.

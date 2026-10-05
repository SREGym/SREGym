# Baseline agent

In each reply the model writes one bash command. The baseline agent runs it in a new shell and sends back the exit code and the output. Any model that LiteLLM can call with an API key works.

## Running

```
export AGENT_API_BASE=https://api.example.com/v1   # your provider's endpoint
export AGENT_API_KEY=...                           # your provider's key
uv run main.py --suite sregym-lite --agent baseline --model openai/<model> \
  --force-build --internet-access open
```

- `--force-build` builds the agent image from your local code. The released image does not have this agent.
- `--internet-access open` is needed for now. In filtered mode the run stops before it starts.
- `--reasoning-effort` is passed to the model as `reasoning_effort`. Without it, the provider's default is used.

## Prompt

The system prompt is based on mini-swe-agent's. The first message is SREGym's task text, one line saying that every command runs in a new shell, and five rules on how to work. After each command the model also sees how many commands and minutes are left in the stage. All the text is in `mini.py`.

mini-swe-agent is under the MIT License, in `LICENSE-mini-swe-agent`.

## Limits

Each stage allows 80 commands and 1500 seconds. Each command can run for 60 seconds. When 15 commands or 5 minutes
are left, the model is told to submit. When a limit is reached, it gets 3 more replies to submit. A stage also ends
after 3 replies in a row without exactly one bash block, or after 2 failed model calls in a row.

## Settings

The agent runs in the agent container, so set these in `agents.yaml` under `kickoff_env`:


| Variable                   | Default | Meaning                           |
| -------------------------- | ------- | --------------------------------- |
| `BASELINE_HARD_CAP`        | 80      | Commands per stage                |
| `BASELINE_DEADLINE_S`      | 1500    | Seconds per stage                 |
| `BASELINE_COMMAND_TIMEOUT` | 60      | Seconds per command               |
| `BASELINE_WRAP_UP_CALLS`   | 3       | Replies allowed after a limit     |
| `BASELINE_MAX_TOKENS`      | 65536   | `max_tokens` for each model call  |
| `BASELINE_TEMPERATURE`     | not set | Sampling temperature              |
| `BASELINE_EXTRA_BODY`      | not set | Extra JSON added to every request |


## Files

The agent writes these files in the run's log folder:


| File                        | Contents                                                                            |
| --------------------------- | ----------------------------------------------------------------------------------- |
| `baseline_transcript.jsonl` | The prompt, every model call, and every command with its exit code, time and output |
| `baseline_results_*.json`   | Token use, command counts, and how each stage ended                                 |
| `steps/step_NN/`            | The messages sent at each step, the reply, and its reasoning                        |


SREGym also writes `trajectory.json` (ATIF), as it does for every agent. Its first two steps are the system prompt and the first message.

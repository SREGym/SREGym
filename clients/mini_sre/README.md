# mini-sre agent

In each reply the model writes one bash command. The mini-sre agent runs it in a new shell and sends back the exit code and the output. Any model that LiteLLM can call with an API key works.

## Running

```
export AGENT_API_BASE=https://api.example.com/v1   # your provider's endpoint
export AGENT_API_KEY=...                           # your provider's key
uv run main.py --suite sregym-lite --agent mini-sre --model openai/<model> --force-build
```

- `--force-build` builds the agent image from your local code. The released image does not have this agent.
- The default filtered internet mode works as for the other agents: the agent can reach its model endpoint and SREGym's own services, and nothing else.
- `--reasoning-effort` is passed to the model as `reasoning_effort`. Without it, the provider's default is used. Where LiteLLM knows the provider's own setting, it translates the value (for Anthropic, `thinking` and `output_config.effort`). For an OpenAI-compatible endpoint it does not know (`openai/<model>` with `AGENT_API_BASE`), the value goes into the request body as it is.

## Prompt

The system prompt is based on mini-swe-agent's. The first message is SREGym's task text, one line saying that every command runs in a new shell, and five rules on how to work. After each command the model also sees how many commands and minutes are left in the stage. All the text is in `mini.py`.

mini-swe-agent is under the MIT License, in `LICENSE-mini-swe-agent`.

## Limits

SREGym stops the agent a set time after it starts (`--agent-timeout`, 1800 seconds by default), for both stages
together. Diagnosis may use up to two thirds of that time (1200 of 1800 seconds); mitigation has the rest, and more
if diagnosis ends sooner. After each command the driver reads the time left from the conductor's `/status`. Each
stage also allows 80 commands, and each command can run for 60 seconds. When 15 commands or 5 minutes are left, the
model is told to submit. When a limit is reached, it gets 3 more replies to submit. A stage also ends
after 3 replies in a row without exactly one bash block, or after 2 failed model calls in a row.

## Settings

The agent runs in the agent container, so set these in `agents.yaml` under `kickoff_env`:


| Variable                   | Default | Meaning                           |
| -------------------------- | ------- | --------------------------------- |
| `MINI_SRE_HARD_CAP`        | 80      | Commands per stage                |
| `MINI_SRE_DIAGNOSIS_SHARE` | 0.667   | Share of the time for diagnosis   |
| `MINI_SRE_ATTEMPT_S`       | 1800    | Time for both stages, if `/status` does not give it |
| `MINI_SRE_COMMAND_TIMEOUT` | 60      | Seconds per command               |
| `MINI_SRE_WRAP_UP_CALLS`   | 3       | Replies allowed after a limit     |
| `MINI_SRE_MAX_TOKENS`      | 65536   | `max_tokens` for each model call  |
| `MINI_SRE_TEMPERATURE`     | not set | Sampling temperature              |
| `MINI_SRE_EXTRA_BODY`      | not set | Extra JSON added to every request |


## Files

The agent writes these files in the run's log folder:


| File                        | Contents                                                                            |
| --------------------------- | ----------------------------------------------------------------------------------- |
| `mini_sre_transcript.jsonl` | The prompt, every model call, and every command with its exit code, time and output |
| `mini_sre_results_*.json`   | Token use, command counts, and how each stage ended                                 |
| `steps/step_NN/`            | The messages sent at each step, the reply, and its reasoning                        |


SREGym also writes `trajectory.json` (ATIF), as it does for every agent. Its first two steps are the system prompt and the first message.

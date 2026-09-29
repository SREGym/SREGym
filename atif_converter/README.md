# Standalone ATIF converter

This folder converts native coding-agent session files into validated Agent
Trajectory Interchange Format (ATIF) v1.7 `Trajectory` objects. It is
self-contained apart from its Pydantic v2 dependency and can be copied into
another Python project without the rest of SREGym.

```python
from atif_converter import convert

trajectory = convert("path/to/session.jsonl")
payload = trajectory.to_json_dict()
```

The converter detects the agent from the file contents. An explicit override
is available when the source is already known:

```python
trajectory = convert("path/to/session.jsonl", agent="codex")
```

## Inputs

| Agent | File to pass |
| --- | --- |
| Claude Code | The primary project session `.jsonl` file under Claude's `projects/` session directory |
| Codex | The rollout/session `.jsonl` file under `$CODEX_HOME/sessions/` |
| Copilot CLI | The structured `copilot-cli.jsonl` produced with `--output-format json` |
| Gemini CLI | A native `session-*.json` or newer `session-*.jsonl` file |
| OpenCode | The `session-*.json` produced by `opencode export` |
| Stratus | The combined `*_stratus_agent_trajectory.jsonl` file |

Supported explicit agent names are `claudecode`, `codex`, `copilot`, `gemini`,
`opencode`, and `stratus`.

Missing paths raise `FileNotFoundError`. Unknown formats and failed conversions
raise subclasses of `AtifConverterError`.

Some Copilot versions omit input and cache counts from the CLI stream.
The converter accepts optional native OpenTelemetry JSONL files from the same run:

```python
trajectory = convert("copilot-cli.jsonl", telemetry_files=["copilot-otel.jsonl"])
```

Telemetry supplies final token counts without adding them to the CLI totals.
The converter ignores duplicate spans and aggregate parent spans.
CLI counts remain available for fields that telemetry does not report.
Without telemetry, conversion works as before. Other agents do not accept this argument.

## Token metrics

Prompt totals include cache reads and writes. Completion totals include
reasoning. Cached tokens are a subset of prompt tokens, not additional tokens.
Reasoning and cache-write counts remain in `extra` when the source reports them.
Unknown counts remain unset; a reported zero remains zero.

New conversions set `final_metrics.extra.token_metrics_version` to `2`.
Earlier conversions did not use consistent token definitions. Existing files
remain unchanged until the caller converts their source recordings again.

Adapters normalize the source counts inside this package. They do not import
SREGym clients or use SREGym result files to calculate token totals.

## Scope

This is an importable source folder, not a separately published distribution.
SREGym keeps its run-directory discovery, metadata enrichment, post-processing,
and SQLite storage outside this folder. The vendored ATIF model provenance and
adapter deviations are recorded in `atif/UPSTREAM.md`.

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

## Scope

This is an importable source folder, not a separately published distribution.
SREGym keeps its run-directory discovery, metadata enrichment, post-processing,
and SQLite storage outside this folder. The vendored ATIF model provenance and
adapter deviations are recorded in `atif/UPSTREAM.md`.

# Vendored ATIF v1.7 models

These Pydantic models are vendored verbatim from the Harbor project's
Agent Trajectory Interchange Format (ATIF) reference implementation.

| Field | Value |
| :-- | :-- |
| **Upstream repo** | https://github.com/harbor-framework/harbor |
| **Source path** | `src/harbor/models/trajectories/` |
| **Commit** | `fd1a8ea6d411b336c9f377aafae1818fe7b18c8d` (2026-06-26) |
| **ATIF version** | v1.7 |
| **RFC** | https://github.com/harbor-framework/harbor/blob/main/rfcs/0001-trajectory-format.md |

## What was changed

The only modification is the import rewrite:

    harbor.models.trajectories  ->  atif_converter.atif

All model logic, field definitions, validators, and `to_json_dict()` are kept
**verbatim**. Do not hand-edit these files; to update, re-vendor from a newer
upstream commit and re-apply the import rewrite, then bump the commit above.

## Files

- `__init__.py` — re-exports
- `agent.py` — `Agent`
- `content.py` — `ContentPart`, `ImageSource`
- `final_metrics.py` — `FinalMetrics`
- `metrics.py` — `Metrics`
- `observation.py` — `Observation`
- `observation_result.py` — `ObservationResult`
- `step.py` — `Step`
- `subagent_trajectory_ref.py` — `SubagentTrajectoryRef`
- `tool_call.py` — `ToolCall`
- `trajectory.py` — `Trajectory` (root model, `extra="forbid"`)

## Ported adapters (`atif_converter/adapters/`)

The per-tool adapters are **clean ports** of Harbor's installed-agent converters
(`src/harbor/agents/installed/`, same upstream commit) into standalone pure
functions with no `harbor` or SREGym dependency. Each adapter accepts the
native agent session file and returns a validated `Trajectory`.

| Adapter | Harbor source | Notes |
| :-- | :-- | :-- |
| `claudecode.py` | `claude_code.py` | session-dir JSONL |
| `codex.py` | `codex.py` | session-dir JSONL; api-call grouping |
| `opencode.py` | `opencode.py` | exported session JSON |
| `copilot.py` | `copilot_cli.py` | `copilot-cli.jsonl`; flat + session-event schemas |
| `gemini.py` | `gemini_cli.py` | archived `sessions/**/session-*.json`; legacy JSON + JSONL |
| `stratus.py` | *(none — bespoke)* | SREGym's own LangGraph agent; see below |

### `stratus.py` is bespoke (no Harbor source)

Stratus is SREGym's own agent, so there is no Harbor converter to port. The
adapter is built from Stratus's emitted trajectory
(`clients/stratus/stratus_agent/driver/driver.py::save_combined_trajectory`):
cumulative LangGraph snapshots (last event per stage = full stage history),
multi-stage (`diagnosis` / `mitigation_attempt_N`) concatenated into one ATIF
trajectory with per-stage boundaries under `extra.stratus.stages`. SREGym's
run converter maps that metadata back to `extra.sregym`. Because it's
our agent, the emitter was extended to serialize `tool_call_id` (id-based tool
matching, with positional fallback for older runs) and `usage_metadata` /
`response_metadata` (per-step token `Metrics`).

### Deliberate deviations from Harbor

- **`copilot.py` emits `ATIF-v1.7`**, not Harbor's hardcoded `ATIF-v1.6` — to
  match the other adapters and the vendored models (the converter uses
  `ObservationResult.extra`, a v1.7 field).
- **`copilot.py` maps reasoning to first-class `reasoning_content`**: real
  Copilot output carries the turn's reasoning in `assistant.message.data.reasoningText`
  (Harbor leaves it in `extra`) and also emits duplicate standalone
  `assistant.reasoning` events (which we skip). Grounded in `results/0704_2011`.

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

    harbor.models.trajectories  ->  sregym.traces.atif

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

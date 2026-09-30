# CloudThinker agent driver

SREGym client for the [CloudThinker](https://github.com/cloudthinker) RCA
platform. It hands each benchmark problem to a running CloudThinker stack, lets
the platform's incident engine diagnose and mitigate it, and submits both stages
to the SREGym conductor so the run is leaderboard-eligible.

The platform runs Anna as a hosted multi-agent system, not as a subprocess the
harness owns, so this client registers with `container_isolation: false` and
talks to the stack through the `ct` dev CLI (`ct chat --json`), which mints the
JWT and streams the turn. Anna's cluster access is a real workspace MCP
connection, not a harness-side tool shim.

## Flow

1. Wait for the conductor to reach the `diagnosis` stage and read the app info
   from `GET /get_app` (application, namespace, incident descriptions).
2. Compose the diagnosis prompt from `CT_PROMPT_MODE` and send it to the agent
   (`ct chat --json`), or, on the `rca_flow` lane, open a CloudThinker incident
   and adopt its RCA run instead.
3. Answer approval gates: a turn parked on a pending tool approval is resumed
   with `ct chat --resume` up to `CT_MAX_APPROVALS` times, including retries for
   a conversation whose previous stream is still open.
4. Submit the diagnosis (`POST /submit`) and write `diagnosis_result.json`.
5. Continue the **same conversation** with the mitigation prompt, so the agent
   keeps its own diagnosis in context, submit the mitigation stage, and write
   `mitigation_result.json`.
6. Read the executed tool calls and the session transcript back from the
   platform, and write `cloudthinker_session.json` for SREGym's ATIF pipeline.

A turn that ends without a tool call is treated as an infrastructure failure, not
a weak answer: the agent can only see the cluster through its MCP connections
(kubectl / Prometheus / Loki / Jaeger), so a diagnosis produced with zero tool
calls was written blind. Those runs write `HARNESS_FAILURE.txt`, submit a
greppable marker, and exit 2 so a chain script can exclude them.

## Prerequisites

1. A running CloudThinker stack, reachable through `ct` from the CloudThinker
   checkout named by `CT_REPO_DIR`.
2. A CloudThinker workspace with an MCP connection to the benchmark cluster.
   Mitigation happens through **that** connection, in-cluster -- not through the
   SREGym agent container -- so a workspace without it can still diagnose, but
   its fixes land nowhere and the mitigation stage grades as failed.
3. `CT_PROMPT_MODE` set by the launching harness: one of `neutral`, `guided`,
   `scoped`, `scoped_rca`, `scoped_rca_corpus`. Each mode is a fixed
   diagnosis/mitigation prompt pair; editing a shipped mode rewrites the meaning
   of every result already labelled with it, so a prompt change ships as a mode.

## Running

Through the harness, which launches the `cloudthinker` entry in `agents.yaml`
and supplies the environment below. The driver has no standalone CLI flags: the
problem identity comes from the harness (`SREGYM_ARTIFACT_ID`) and the conductor.

## Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `API_HOSTNAME` | `localhost` | SREGym conductor host (set by the harness). |
| `API_PORT` | `8000` | SREGym conductor port (set by the harness). |
| `SREGYM_ARTIFACT_ID` | — | Problem identity for artifact naming (set by the harness; `SREGYM_PROBLEM_ID` as legacy fallback). |
| `AGENT_LOGS_DIR` | `./logs/cloudthinker` | Directory for `driver.log`, the per-stage artifacts, and the session file. The harness points this at the run directory. |
| `CT_BIN` | `ct` | The CloudThinker dev CLI. |
| `CT_REPO_DIR` | — | CloudThinker checkout to run `ct` from; `ct` resolves its compose slot from the working directory. |
| `CT_WORKSPACE` | — | Workspace the run belongs to. |
| `CT_SELECTION` | — | Model assignment for the tier, e.g. `model:bedrock/global.anthropic.claude-opus-5`. |
| `CT_PROMPT_MODE` | — | Required. Prompt pair, see Prerequisites. |
| `CT_LANE` | `chat` | `chat` drives the agent directly; `rca_flow` opens a platform incident and adopts its RCA run. |
| `CT_TURN_TIMEOUT` | `900` | Seconds allowed for one chat turn. |
| `CT_MAX_APPROVALS` | `8` | Approval gates answered per stage. |
| `CT_TURN_RETRIES` / `CT_TURN_BACKOFF` | `2` / `30` | Retries and backoff for a failed turn. |
| `CT_RESUME_RETRIES` / `CT_RESUME_BACKOFF` / `CT_RESUME_POLL_SECONDS` | `9` / `5` / `15` | Retries while a previous stream is still open. |
| `CT_GATE_SETTLE_SECONDS` | `5` | Wait after a turn before checking for a pending approval. |
| `CT_STAGE_WAIT` | `900` | Seconds to wait for the conductor to leave the diagnosis stage. |
| `CT_RCA_SCRIPT` | `<CT_REPO_DIR>/.agents/skills/sregym-bench-internal/scripts/rca_run.py` | Incident runner used by the `rca_flow` lane. |
| `CT_RCA_PYTHON` | `<CT_REPO_DIR>/backend/.venv/bin/python3` | Interpreter for `CT_RCA_SCRIPT` (needs the platform's Python dependencies). |
| `CT_RCA_TIMEOUT` | `1800` | Seconds allowed for one incident RCA run. |
| `CT_TRANSCRIPT_TIMEOUT` | `120` | Seconds allowed for one transcript read. |
| `CT_MEMORY_ARM` and `CT_MEMORY_SEEDED_*` | — | Recall-arm label and seeded counts, logged so a warm run that could recall nothing stays distinguishable from a cold one. |

## Session artifact

`cloudthinker_session.json` in the run directory is the source for SREGym's ATIF
trajectory (`atif_converter/adapters/cloudthinker.py`):

```json
{
  "schema": "cloudthinker_session/v1",
  "problem_id": "...",
  "selection": "model:...",
  "prompt_mode": "scoped",
  "stages": [
    {"name": "diagnosis", "conversation_id": "...", "submitted": "...", "rows_at_submission": 30},
    {"name": "mitigation", "conversation_id": "...", "submitted": ""}
  ],
  "conversations": [{"conversation_id": "...", "records": []}]
}
```

`records` holds one row per message component (TEXT, THINKING, TOOL) read from
the platform's own tables, ordered by `(created_at, position)`. The mitigation
stage resumes the diagnosis conversation, so the transcript is attached once per
conversation and `rows_at_submission` -- the row count when that stage submitted
-- is what splits the diagnosis steps from the mitigation steps.

## Artifacts

| File | Content |
| --- | --- |
| `driver.log` | Driver log, including the executed tool calls per stage. |
| `diagnosis_result.json`, `mitigation_result.json` | Stage answer, conversation id, executed tools, approvals. |
| `summary.json` | Problem id, driver name, timestamp. |
| `cloudthinker_session.json` | Session transcript for the ATIF trajectory. |
| `rca_incident_body.txt`, `rca_run.json` | Incident body and RCA run record (`rca_flow` lane only). |
| `HARNESS_FAILURE.txt` | Present only on an infrastructure failure; the run must be excluded, not scored. |

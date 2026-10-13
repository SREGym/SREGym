"""Adapt the standard two-stage agent instruction to the stages a problem has."""

import re

_WORKFLOW = re.compile(r"WORKFLOW: You will perform TWO tasks in sequence:.*?(?=\nImportant:)", re.S)

MITIGATION_ONLY = """WORKFLOW: This problem has a single task: MITIGATION. There is no diagnosis stage.

TASK: MITIGATION
- End users of the application are experiencing degraded service
- Investigate the application (metrics, logs, traces, cluster state) and identify the root cause
- Implement a fix to resolve the problem autonomously (do not ask for confirmation)
- After applying the fix, YOU MUST submit with an empty string to trigger validation
- The submission is REQUIRED - do not exit without submitting, and submit only once
- Your mitigation is evaluated on whether end-user requests succeed with normal latency for a sustained period after you submit

HOW TO SUBMIT:

The submission endpoint is: {api}/submit
- After applying your fix, POST {api}/submit with JSON: {{"solution": ""}}
- Do NOT submit a diagnosis or any non-empty text; the first submission is graded as your mitigation
"""


def adapt_instruction(instruction: str, app_info: dict, api_base: str) -> str:
    """Replace the diagnosis+mitigation workflow when the problem is mitigation-only."""
    if app_info.get("stages") != ["mitigation"]:
        return instruction
    return _WORKFLOW.sub(lambda _: MITIGATION_ONLY.format(api=api_base), instruction, count=1)

"""Build the workflow for mitigation-only problems before assembling a prompt."""

MITIGATION_ONLY = """WORKFLOW: This problem has a single task: MITIGATION. There is no diagnosis stage.

TASK: MITIGATION
- End users of the application are experiencing degraded service
- Investigate the application (metrics, logs, traces, cluster state) and identify the root cause
- Implement a fix to resolve the problem autonomously (do not ask for confirmation)
- After applying the fix, YOU MUST submit your mitigation to trigger validation
- The submission is REQUIRED - do not exit without submitting, and submit only once
- Your mitigation is evaluated on whether end-user requests succeed with normal latency for a sustained period after you submit

{submission_instruction}
"""

HTTP_SUBMISSION = """HOW TO SUBMIT:

The submission endpoint is: {api}/submit
- After applying your fix, POST {api}/submit with JSON: {{"solution": ""}}
- Do NOT submit a diagnosis or any non-empty text; the first submission is graded as your mitigation
"""


def mitigation_workflow(api_base: str, *, submission_instruction: str | None = None) -> str:
    """Select submission instructions without rewriting an existing prompt."""
    submission = submission_instruction or HTTP_SUBMISSION.format(api=api_base)
    return MITIGATION_ONLY.format(submission_instruction=submission.rstrip("\n")).rstrip("\n")

"""Prompt text and message formats for the mini-sre agent.

The texts follow mini-swe-agent v1.17.5 (agents/default.py and config/mini.yaml). The system
prompt and the observation, long-output, format error and timeout messages are copied or adapted
from mini-swe-agent. Its license is in LICENSE-mini-swe-agent in this folder.
"""

from __future__ import annotations

import re

# Only used in the format error message, which is copied from mini-swe-agent.
MARKER = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
CURL_INSTANCE_NOTE = """

Note: every command is executed in a new subshell; directory or environment variable changes do not persist."""
ACTION_REGEX = re.compile(r"```bash\s*\n(.*?)\n```", re.DOTALL)
OUTPUT_LIMIT = 10_000  # mini-swe-agent's observation limit: head and tail of 5,000 characters each
MAX_CONSECUTIVE_FORMAT_ERRORS = 3

SYSTEM_TEMPLATE = """You are a helpful assistant that can interact with a computer.
Your response must contain exactly ONE bash code block with ONE command (or commands connected with && or ||).
Include a THOUGHT section before your command where you explain your reasoning process.
Format your response as shown in <format_example>.

<format_example>
Your reasoning and analysis here. Explain why you want to perform the action.

```bash
your_command_here
```
</format_example>

Failure to follow these rules will cause your response to be rejected."""

# General rules on how to investigate. They do not name any Kubernetes object or fault type.
WORKFLOW_TEXT = """

## How to work

1. Before each command, say what you currently think is wrong and what this command would tell you.
   A command whose result cannot change your mind is not worth running.
2. Read the output you asked for before asking for more, and say what it rules in and what it rules out.
3. If two commands in a row tell you nothing new about the current idea, drop it and try another one.
4. Survey the whole system before going deep into any one part of it.
5. Stop as soon as your evidence answers the task. Evidence gathered after that point cannot improve your
   answer, and the stage ends whether or not you have submitted."""

ACTION_OBSERVATION_TEMPLATE = """<returncode>{returncode}</returncode>
{body}"""

OUTPUT_TOO_LONG_WARNING = """<warning>
The output of your last command was too long.
Please try a different command that produces less output.
If you're looking at a file you can try use head, tail or sed to view a smaller number of lines selectively.
If you're using grep or find and it produced too much output, you can use a more selective search pattern.
If you really need to see something from the full command's output, you can redirect output to a file and then search in that file.
</warning>"""

FORMAT_ERROR_TEMPLATE = """Please always provide EXACTLY ONE action in triple backticks, found {n} actions.
If you want to end the stage, issue the submission command described in the task (its output must start with `{marker}`)
without any other command.
Else, please format your response exactly as follows:
<response_example>
Here are some thoughts about why you want to perform the action.

```bash
<action>
```
</response_example>
Note: In rare cases, if you need to reference a similar format in your command, you might have
to proceed in two steps, first writing TRIPLEBACKTICKSBASH, then replacing them with ```bash."""

TIMEOUT_TEMPLATE = """The last command <command>{action}</command> timed out and has been killed.
The output of the command was:
{body}
Please try another command and make sure to avoid those requiring interactive input."""

COUNTDOWN_COMMANDS = 15  # the last stretch before the cap, where the notice turns urgent
COUNTDOWN_SECONDS = 300  # ... and the last stretch before the stage deadline

BUDGET_LINE_TEMPLATE = "\n<budget>command {used} of {cap}, about {minutes} minutes left in this stage</budget>"

LIMIT_NOTICE_TEMPLATE = """
<IMPORTANT>
You have {left} left in this stage. When that runs out the stage ends, and if you have not submitted by then,
nothing is recorded for it. Stop pulling on whatever thread you are on and come back to the big picture:
submit your best answer now. {how}
</IMPORTANT>"""

WRAP_UP_TEMPLATE = """You have reached {reason} for this stage. Stop investigating and submit your best answer now,
based on what you already know. {how}"""

WRAP_UP_HOW = "Submit it to the conductor exactly as the task instruction describes."


def build_task_text(app_info: dict) -> str:
    """SREGym's task text, built by clients.codex.driver.build_instruction."""
    from clients.codex.driver import build_instruction

    return build_instruction(app_info)


def system_text() -> str:
    """Return the system prompt."""
    return SYSTEM_TEMPLATE


def instance_text(app_info: dict) -> str:
    """First user message: the task text, the subshell note and the workflow rules."""
    return build_task_text(app_info).rstrip() + CURL_INSTANCE_NOTE + WORKFLOW_TEXT


def accepted_submission(output: str, stage: str) -> bool:
    """Check whether the output is the conductor's reply accepting a submission for this stage."""
    return bool(
        re.search(r'"message"\s*:\s*"Submission received"', output or "")
        and re.search(rf'"stage"\s*:\s*"{re.escape(stage)}"', output or "")
    )


def parse_action(content: str) -> tuple[str | None, int]:
    """Return the command and the number of bash blocks. The command is None unless there is exactly one."""
    actions = ACTION_REGEX.findall(content or "")
    if len(actions) == 1:
        return actions[0].strip(), 1
    return None, len(actions)


def format_error_text(n_actions: int) -> str:
    """Return the message for a reply without exactly one bash block."""
    return FORMAT_ERROR_TEMPLATE.format(n=n_actions, marker=MARKER)


def _output_body(output: str) -> str:
    """Wrap a command's output, keeping the first and last 5,000 characters of a long one."""
    if len(output) < OUTPUT_LIMIT:
        return f"<output>\n{output}\n</output>"
    elided = len(output) - OUTPUT_LIMIT
    return (
        f"{OUTPUT_TOO_LONG_WARNING}\n<output_head>\n{output[:5000]}\n</output_head>\n<elided_chars>\n{elided} characters elided\n"
        f"</elided_chars>\n<output_tail>\n{output[-5000:]}\n</output_tail>"
    )


def observation_text(returncode: int, output: str) -> str:
    """Return the message sent after a command: its exit code and output."""
    return ACTION_OBSERVATION_TEMPLATE.format(returncode=returncode, body=_output_body(output))


def timeout_text(action: str, output: str) -> str:
    """Return the message sent after a command that timed out."""
    return TIMEOUT_TEMPLATE.format(action=action, body=_output_body(output))


def budget_notice(*, used: int, cap: int, seconds_left: float) -> str:
    """Line added after each command with the commands and time left.

    In the last COUNTDOWN_COMMANDS commands or COUNTDOWN_SECONDS seconds it becomes a message to submit.
    """
    commands_left = max(cap - used, 0)
    minutes = max(int(seconds_left // 60), 0)
    urgent = []
    if commands_left <= COUNTDOWN_COMMANDS:
        urgent.append(f"{commands_left} command{'' if commands_left == 1 else 's'}")
    if seconds_left <= COUNTDOWN_SECONDS:
        urgent.append(f"about {minutes} minute{'' if minutes == 1 else 's'}")
    if urgent:
        return LIMIT_NOTICE_TEMPLATE.format(left=" and ".join(urgent), how=WRAP_UP_HOW)
    return BUDGET_LINE_TEMPLATE.format(used=used, cap=cap, minutes=minutes)


def wrap_up_text(reason: str) -> str:
    """Message sent once when the stage hits a limit."""
    return WRAP_UP_TEMPLATE.format(reason=reason, how=WRAP_UP_HOW)

#!/usr/bin/env python3

import argparse
from typing import Annotated, Union

from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command

from clients.langgraph_agent.tools.text_editing.file_manip import update_file_vars_in_state

RETRY_WITH_OUTPUT_TOKEN = "###SWE-AGENT-RETRY-WITH-OUTPUT###"

_LINT_ERROR_TEMPLATE = """Your proposed edit has introduced new syntax error(s).
Please read this error message carefully and then retry editing the file.

ERRORS:

{errors}

This is how your edit would have looked if applied
------------------------------------------------
{window_applied}
------------------------------------------------

This is the original code before your edit
------------------------------------------------
{window_original}
------------------------------------------------

Your changes have NOT been applied. Please fix your edit command and try again.
DO NOT re-run the same failed edit command. Running it again will lead to the same error.
"""


def get_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("text", type=str)
    parser.add_argument("line", type=int, nargs="?", default=None)
    return parser


@tool("insert")
def insert(
    state: Annotated[dict, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    text: str,
    line: Union[int, None] = None,
):
    """
    Insert <text> at the end of the currently opened file or after <line> if specified.
    """
    if len(state["curr_file"]) == 0:
        msg_txt = "No file opened. Either `open` or `create` a file first."
        return Command(update=update_file_vars_in_state(state, msg_txt, tool_call_id))
    wf = WindowedFile(state["curr_file"])

    pre_edit_lint = flake8(wf.path)
    insert_info = wf.insert(text, line=line - 1 if line is not None else None)
    post_edit_lint = flake8(wf.path)

    # Try to filter out pre-existing errors
    replacement_window = (insert_info.first_inserted_line, insert_info.first_inserted_line)
    new_flake8_output = format_flake8_output(
        post_edit_lint,
        previous_errors_string=pre_edit_lint,
        replacement_window=replacement_window,
        replacement_n_lines=insert_info.n_lines_added,
    )

    if new_flake8_output:
        with_edits = wf.get_window_text(line_numbers=True, status_line=True, pre_post_line=True)
        wf.undo_edit()
        without_edits = wf.get_window_text(line_numbers=True, status_line=True, pre_post_line=True)
        msg_txt = _LINT_ERROR_TEMPLATE.format(
            errors=new_flake8_output, window_applied=with_edits, window_original=without_edits
        )
        return Command(update=update_file_vars_in_state(state, msg_txt, tool_call_id))

    msg_txt = wf.get_window_text(line_numbers=True, status_line=True, pre_post_line=True)
    return Command(update=update_file_vars_in_state(state, msg_txt, tool_call_id))

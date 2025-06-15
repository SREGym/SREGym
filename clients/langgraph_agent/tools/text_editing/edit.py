#!/usr/bin/env python3

import argparse
from typing import Annotated

from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command

from clients.langgraph_agent.tools.text_editing.file_manip import update_file_vars_in_state

try:
    from sweagent import TOOLS_DIR
except ImportError:
    pass
else:
    import sys

    default_lib = TOOLS_DIR / "defaults" / "lib"
    assert default_lib.is_dir()
    sys.path.append(str(default_lib))
    sys.path.append(str(TOOLS_DIR / "registry" / "lib"))

from flake8_utils import flake8, format_flake8_output  # type: ignore
from windowed_file import FileNotOpened, TextNotFound, WindowedFile  # type: ignore

RETRY_WITH_OUTPUT_TOKEN = "###SWE-AGENT-RETRY-WITH-OUTPUT###"

_NOT_FOUND = """Your edit was not applied (file not modified): Text {search!r} not found in displayed lines (or anywhere in the file).
Please modify your search string. Did you forget to properly handle whitespace/indentation?
You can also call `open` again to re-display the file with the correct context.
"""

_NOT_FOUND_IN_WINDOW_MSG = """Your edit was not applied (file not modified): Text {search!r} not found in displayed lines.

However, we found the following occurrences of your search string in the file:

{occurrences}

You can use the `goto` command to navigate to these locations before running the edit command again.
"""

_MULTIPLE_OCCURRENCES_MSG = """Your edit was not applied (file not modified): Found more than one occurrence of {search!r} in the currently displayed lines.
Please make your search string more specific (for example, by including more lines of context).
"""

_NO_CHANGES_MADE_MSG = """Your search and replace strings are the same. No changes were made. Please modify your search or replace strings."""

_SINGLE_EDIT_SUCCESS_MSG = """Text replaced. Please review the changes and make sure they are correct:

1. The edited file is correctly indented
2. The edited file does not contain duplicate lines
3. The edit does not break existing functionality

Edit the file again if necessary."""

_MULTIPLE_EDITS_SUCCESS_MSG = """Replaced {n_replacements} occurrences. Please review the changes and make sure they are correct:

1. The edited file is correctly indented
2. The edited file does not contain duplicate lines
3. The edit does not break existing functionality

Edit the file again if necessary."""

_LINT_ERROR_TEMPLATE = """Your proposed edit has introduced new syntax error(s). Please read this error message carefully and then retry editing the file.

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
    parser.add_argument("search", type=str)
    parser.add_argument("replace", type=str)
    parser.add_argument("replace_all", type=bool, nargs="?", default=False)
    return parser


@tool("edit")
def edit(
    state: Annotated[dict, InjectedState] = None,
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    search: str = "",
    replace: str = "",
    replace_all: bool = "",
) -> Command:
    """
    Replace first occurrence of <search> with <replace> in the currently displayed lines.
    If replace-all is True , replace all occurrences of <search> with <replace>.

    For example, if you are looking at this file:

    def fct():
        print("Hello world")

    and you want to edit the file to read:

    def fct():
        print("Hello")
        print("world")

    you can search for `Hello world` and replace with `"Hello"\n    print("world")`
    (note the extra spaces before the print statement!).

    Tips:

    1. Always include proper whitespace/indentation
    2. When you are adding an if/with/try statement, you need to INDENT the block that follows, so make sure to include it in both your search and replace strings!
    3. If you are wrapping code in a try statement, make sure to also add an 'except' or 'finally' block.

    Before every edit, please

    1. Explain the code you want to edit and why it is causing the problem
    2. Explain the edit you want to make and how it fixes the problem
    3. Explain how the edit does not break existing functionality
    """
    if not isinstance(state["curr_file"], str):
        logger.error("INTERNAL: state curr file should be a string")
        exit(1)
    if len(state["curr_file"]) == 0:
        msg_txt = "No file opened. Either `open` or `create` a file first."
        return Command(update=update_file_vars_in_state(state, msg_txt, tool_call_id))

    wf = WindowedFile(path=state["curr_file"])

    # Turn \\n into \n etc., i.e., undo the escaping
    # args.replace = args.replace.encode("utf8").decode("unicode_escape")

    if search == replace:
        msg_txt = _NO_CHANGES_MADE_MSG + "\n" + RETRY_WITH_OUTPUT_TOKEN
        return Command(update=update_file_vars_in_state(state, msg_txt, tool_call_id))

    pre_edit_lint = flake8(wf.path)

    try:
        if not replace_all:
            window_text = wf.get_window_text()
            if window_text.count(search) > 1:
                msg_txt = _MULTIPLE_OCCURRENCES_MSG.format(search=search) + "\n" + RETRY_WITH_OUTPUT_TOKEN
                return Command(update=update_file_vars_in_state(state, msg_txt, tool_call_id))
            replacement_info = wf.replace_in_window(search, replace)
            # todo: Should warn if more than one occurrence was found?
        else:
            # todo: Give overview of all replaced occurrences/number of replacements
            replacement_info = wf.replace(search, replace)
    except TextNotFound:
        line_no_founds = wf.find_all_occurrences(search, zero_based=False)
        if line_no_founds:
            msg_txt = _NOT_FOUND_IN_WINDOW_MSG.format(
                search=search, occurrences="\n".join([f"- line {line_no}" for line_no in line_no_founds])
            )
        else:
            msg_txt = _NOT_FOUND.format(search=search)
        msg_txt = msg_txt + "\n" + RETRY_WITH_OUTPUT_TOKEN
        return Command(update=update_file_vars_in_state(state, msg_txt, tool_call_id))

    post_edit_lint = flake8(wf.path)

    if not replace_all:
        # Try to filter out pre-existing errors
        replacement_window = (
            replacement_info.first_replaced_line,
            replacement_info.first_replaced_line + replacement_info.n_search_lines - 1,
        )
        new_flake8_output = format_flake8_output(
            post_edit_lint,
            previous_errors_string=pre_edit_lint,
            replacement_window=replacement_window,
            replacement_n_lines=replacement_info.n_replace_lines,
        )
    else:
        # Cannot easily compare the error strings, because line number changes are hard to keep track of
        # So we show all linter errors.
        new_flake8_output = format_flake8_output(post_edit_lint)

    if new_flake8_output:
        with_edits = wf.get_window_text(line_numbers=True, status_line=True, pre_post_line=True)
        wf.undo_edit()
        without_edits = wf.get_window_text(line_numbers=True, status_line=True, pre_post_line=True)
        msg_txt = _LINT_ERROR_TEMPLATE.format(
            errors=new_flake8_output,
            window_applied=with_edits,
            window_original=without_edits,
        )
        msg_txt = msg_txt + "\n" + RETRY_WITH_OUTPUT_TOKEN
        return Command(update=update_file_vars_in_state(state, msg_txt, tool_call_id))
    if not replace_all:
        msg_txt = _SINGLE_EDIT_SUCCESS_MSG
    else:
        msg_txt = _MULTIPLE_EDITS_SUCCESS_MSG.format(n_replacements=replacement_info.n_replacements)

    msg_txt = msg_txt + "\n\n" + wf.get_window_text(line_numbers=True, status_line=True, pre_post_line=True)
    return Command(update=update_file_vars_in_state(state, msg_txt, tool_call_id))

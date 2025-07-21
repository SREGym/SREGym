import logging
import os.path
from pathlib import Path
from typing import Annotated, Optional, Union

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command

from clients.langgraph_agent.state import State
from clients.langgraph_agent.tools.text_editing.flake8_utils import flake8, format_flake8_output  # type: ignore
from clients.langgraph_agent.tools.text_editing.windowed_file import (  # type: ignore
    FileNotOpened,
    TextNotFound,
    WindowedFile,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def update_file_vars_in_state(
    state: State,
    message: str | ToolMessage | AIMessage | HumanMessage,
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
) -> State:
    logger.info("updating state with message: %s", message)
    logger.info(f"state: {state}, tool_call_id: {tool_call_id}")
    new_state = state

    match message:
        case str():
            logger.info("Not updating state as message is a string")
            new_state["messages"] = new_state["messages"] + [ToolMessage(content=message, tool_call_id=tool_call_id)]
        case ToolMessage():
            logger.info("Trying to update states with message as ToolMessage")
            tool_call_msg = ""
            for i in range(len(new_state["messages"]) - 1, -1, -1):
                if hasattr(new_state["messages"][i], "tool_calls") and len(new_state["messages"][i].tool_calls) > 0:
                    tool_call_msg = new_state["messages"][i]
                    logger.info("Found last tool call message: %s", tool_call_msg)
                    break
            tool_name = tool_call_msg.tool_calls[0]["name"]
            tool_args = tool_call_msg.tool_calls[0]["args"]
            logger.info("Found tool args: %s", tool_args)
            if tool_name == "open_file":
                new_state["curr_file"] = tool_args["path"]
                new_state["curr_line"] = tool_args["line_number"]
                new_state["workdir"] = str(Path(tool_args["path"]).parent)
            elif tool_name == "goto_line":
                new_state["curr_line"] = tool_args["line_number"]
            elif tool_name == "create":
                new_state["curr_file"] = tool_args["path"]
                new_state["workdir"] = str(Path(tool_args["path"]).parent)
            elif tool_name == "edit":
                # Explicitly pointing out as this tool does not modify agent state
                pass
            elif tool_name == "insert":
                # Explicitly pointing out as this tool does not modify agent state
                pass

            new_state["messages"] = new_state["messages"] + [message]
        case _:
            logger.info("Not found open_file or goto_line in message: %s", message)
            logger.info("Not updating state")
    logger.info("Updated state: %s", new_state)
    return new_state


@tool("open_file", description="open a file, path: <absolute path to file>, line_number: <line_number>")
def open_file(
    state: Annotated[dict, InjectedState] = None,
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    path: Optional[str] = None,
    line_number: Optional[str] = None,
) -> Command:
    logger.info("in open_file, the last msg: %s", state["messages"][-1])
    if path is None:
        msg_txt = 'Usage: open "<file>" [<line_number>]'
        return Command(
            update=update_file_vars_in_state(state, msg_txt, tool_call_id),
        )

    if not os.path.exists(path):
        msg_txt = f"Error: File '{path}' does not exist."
        return Command(
            update=update_file_vars_in_state(state, msg_txt, tool_call_id),
        )

    wf = WindowedFile(path=Path(path), exit_on_exception=False)

    if line_number is not None:
        try:
            line_num = int(line_number)
        except ValueError:
            msg_txt = 'Usage: open "<file>" [<line_number>]' + "Error: <line_number> must be a number"
            return Command(
                update=update_file_vars_in_state(state, msg_txt, tool_call_id),
            )
        if line_num > wf.n_lines:
            msg_txt = (
                f"Warning: <line_number> ({line_num}) is greater than the number of lines in the file ({wf.n_lines})"
                + f"Warning: Setting <line_number> to {wf.n_lines}"
            )
            line_num = wf.n_lines
            return Command(
                update=update_file_vars_in_state(state, msg_txt, tool_call_id),
            )
        elif line_num < 1:
            msg_txt = f"Warning: <line_number> ({line_num}) is less than 1" + "Warning: Setting <line_number> to 1"
            line_num = 1
            return Command(
                update=update_file_vars_in_state(state, msg_txt, tool_call_id),
            )
    else:
        # Default to middle of window if no line number provided
        line_num = wf.first_line

    wf.goto(line_num - 1, mode="top")
    msg_txt = wf.get_window_text(line_numbers=True, status_line=True, pre_post_line=True)
    return Command(
        update=update_file_vars_in_state(
            state,
            ToolMessage(content=msg_txt, tool_call_id=tool_call_id),
        ),
    )


@tool("goto_line", description="goto a line in an opened file, line_number: <line_number>")
def goto_line(
    state: Annotated[dict, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    line_number: Optional[int] = None,
) -> Command:
    if state["curr_file"] == "":
        msg_txt = "Error: No file is open, use open_file to open a file first"
        return Command(
            update=update_file_vars_in_state(state, msg_txt, tool_call_id),
        )

    if line_number is None:
        msg_txt = "Usage: goto <line_number>"
        return Command(
            update=update_file_vars_in_state(state, msg_txt, tool_call_id),
        )

    try:
        line_number = int(line_number)
    except ValueError:
        msg_txt = "Usage: goto <line>\n" + "Error: <line> must be a number"
        return Command(
            update=update_file_vars_in_state(state, msg_txt, tool_call_id),
        )

    curr_file = str(state["curr_file"])
    wf = WindowedFile(curr_file)

    if line_number > wf.n_lines:
        msg_txt = f"Error: <line> must be less than or equal to {wf.n_lines}"
        return Command(
            update=update_file_vars_in_state(state, msg_txt, tool_call_id),
        )

    # Convert from 1-based line numbers (user input) to 0-based (internal representation)
    wf.goto(line_number - 1, mode="top")
    wf.print_window()
    msg_txt = wf.get_window_text(line_numbers=True, status_line=True, pre_post_line=True)
    return Command(
        update=update_file_vars_in_state(
            state,
            ToolMessage(content=msg_txt, tool_call_id=tool_call_id),
        )
    )


@tool("create", description="Create a new file. path: <path to new file>")
def create(state: Annotated[dict, InjectedState], tool_call_id: Annotated[str, InjectedToolCallId], path: str):
    path = Path(path)
    if path.exists():
        msg_txt = f"Warning: File '{path}' already exists."
        return Command(
            update=update_file_vars_in_state(
                state,
                ToolMessage(content=msg_txt, tool_call_id=tool_call_id),
            )
        )

    path.write_text("\n")

    wf = WindowedFile(path=path)
    wf.first_line = 0
    wf.print_window()
    msg_txt = "File created successfully."
    return Command(
        update=update_file_vars_in_state(
            state,
            ToolMessage(content=msg_txt, tool_call_id=tool_call_id),
        )
    )


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
        msg_txt = _NO_CHANGES_MADE_MSG
        return Command(update=update_file_vars_in_state(state, msg_txt, tool_call_id))

    pre_edit_lint = flake8(wf.path)

    try:
        if not replace_all:
            window_text = wf.get_window_text()
            if window_text.count(search) > 1:
                msg_txt = _MULTIPLE_OCCURRENCES_MSG.format(search=search)
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
        msg_txt = msg_txt
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
        msg_txt = msg_txt
        return Command(update=update_file_vars_in_state(state, msg_txt, tool_call_id))
    if not replace_all:
        msg_txt = _SINGLE_EDIT_SUCCESS_MSG
    else:
        msg_txt = _MULTIPLE_EDITS_SUCCESS_MSG.format(n_replacements=replacement_info.n_replacements)

    msg_txt = msg_txt + "\n\n" + wf.get_window_text(line_numbers=True, status_line=True, pre_post_line=True)
    return Command(update=update_file_vars_in_state(state, msg_txt, tool_call_id))


@tool("insert")
def insert(
    state: Annotated[dict, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    text: str,
    line_number: Union[int, None] = None,
):
    """
    Insert <text> at the end of the currently opened file or after <line> if specified.
    """
    if len(state["curr_file"]) == 0:
        msg_txt = "No file opened. Either `open` or `create` a file first."
        return Command(update=update_file_vars_in_state(state, msg_txt, tool_call_id))
    wf = WindowedFile(state["curr_file"])

    pre_edit_lint = flake8(wf.path)
    insert_info = wf.insert(text, line=line_number - 1 if line_number is not None else None)
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

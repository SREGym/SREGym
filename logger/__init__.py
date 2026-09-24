import logging
import os
from datetime import datetime

from rich.console import Console
from rich.logging import RichHandler

from .handler import ExhaustInfoFormatter

# Shared Console used by both the logging stack and any rich.Live displays
# (e.g. the benchmark Progress bar in main.py). Routing both through the same
# Console lets Rich keep the bar anchored at the bottom while log lines stream
# above it — separate Console instances would tear through the live region.
console = Console()


def get_current_datetime_formatted():
    now = datetime.now()
    formatted_datetime = now.strftime("%m%d_%H%M")
    return formatted_datetime


def init_logger():
    # set up the logger for log file
    root_logger = logging.getLogger("all")
    root_logger.setLevel(logging.DEBUG)
    root_logger.propagate = False  # do not propagate to the real root logger ('')

    # Only add handlers if they don't exist to prevent duplication
    if not root_logger.handlers:
        timestamp = get_current_datetime_formatted()
        # create dir and file
        log_dir = os.environ.get("AGENT_LOGS_DIR", "./logs")
        path = f"{log_dir}/sregym_{timestamp}.log"
        os.makedirs(log_dir, exist_ok=True)

        handler = logging.FileHandler(path)
        # add code line and filename and function name
        handler.setFormatter(
            ExhaustInfoFormatter(
                fmt="%(asctime)s - %(levelname)s - %(name)s - %(message)s - %(filename)s:%(funcName)s:%(lineno)d",
                datefmt="%Y-%m-%d %H:%M:%S",
                extra_attributes=["sol", "result", "Full Prompt", "Tool Calls"],
            )
        )
        handler.setLevel(logging.DEBUG)
        root_logger.addHandler(handler)

        rich_handler = RichHandler(
            console=console,
            show_time=True,
            show_level=True,
            show_path=False,
            rich_tracebacks=True,
            markup=False,
        )
        rich_handler.setFormatter(logging.Formatter(fmt="%(name)s - %(message)s"))
        rich_handler.setLevel(logging.INFO)
        root_logger.addHandler(rich_handler)

    unify_third_party_loggers()
    silent_litellm_loggers()
    silent_httpx_loggers()
    silent_FastMCP_loggers()


def silent_paramiko_loggers():
    # make the paramiko logger silent
    logging.getLogger("paramiko").setLevel(logging.WARNING)  # throttle the log source


def silent_FastMCP_loggers():
    # make the FastMCP logger silent
    logging.getLogger("mcp").setLevel(logging.WARNING)


def silent_litellm_loggers():
    verbose_proxy_logger = logging.getLogger("LiteLLM Proxy")
    verbose_router_logger = logging.getLogger("LiteLLM Router")
    verbose_logger = logging.getLogger("LiteLLM")
    verbose_proxy_logger.setLevel(logging.WARNING)
    verbose_router_logger.setLevel(logging.WARNING)
    verbose_logger.setLevel(logging.WARNING)


def silent_httpx_loggers():
    httpx_logger = logging.getLogger("httpx")
    httpx_logger.setLevel(logging.WARNING)


def unify_third_party_loggers():
    """Replace any handlers on the real root logger (used by uvicorn et al.)
    with a RichHandler bound to the shared console, so third-party log output
    flows through the same live-display-aware sink as our own logger."""
    root = logging.getLogger("")
    # Drop existing handlers — they hold references to sys.stderr captured
    # before any rich.Live was active, and would tear through the progress bar.
    root.handlers = []

    rich_handler = RichHandler(
        console=console,
        show_time=True,
        show_level=True,
        show_path=False,
        rich_tracebacks=True,
        markup=False,
    )
    rich_handler.setFormatter(logging.Formatter(fmt="%(name)s - %(message)s"))
    rich_handler.setLevel(logging.INFO)
    root.addHandler(rich_handler)


# silent uvicorn: main.py:96

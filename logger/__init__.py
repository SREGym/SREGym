import logging
import os
from datetime import datetime
from logging import Formatter

from .handler import ColorFormatter, ExhaustInfoFormatter


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
        path = f"./logs/sregym_{timestamp}.log"
        os.makedirs("./logs", exist_ok=True)

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

        std_handler = logging.StreamHandler()
        std_handler.setFormatter(
            ColorFormatter(fmt="%(levelname)s - %(name)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S", style="%")
        )
        std_handler.setLevel(logging.INFO)
        root_logger.addHandler(std_handler)

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
    # make the info level third party loggers (e.g. paramiko) have the common formatter
    logging.getLogger("")
    # get the handler
    handlers = logging.getLogger("").handlers
    if handlers:
        for handler in handlers:
            handler.setFormatter(
                ColorFormatter(fmt="%(levelname)s - %(name)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S", style="%")
            )
    else:
        print("No handler found for root logger")


# silent uvicorn: main.py:96

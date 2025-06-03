# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Utility functions for handling status and errors."""

from enum import Enum
from colorama import Fore, Style

from aiopslab.config import Config
from aiopslab.paths import BASE_DIR

config = Config(BASE_DIR / "config.yml")


class SubmissionStatus(Enum):
    VALID_SUBMISSION = 1
    INVALID_SUBMISSION = 2


class InvalidActionError(Exception):
    def __init__(self, action_name):
        super().__init__(f"Invalid action: {action_name}")
        self.action_name = action_name


class ResponseParsingError(Exception):
    def __init__(self, message):
        super().__init__(f"Error parsing response: {message}")
        self.message = message


class SessionPrint:
    def __init__(self):
        self.enable_printing = config.get("print_session")

    def agent(self, action):
        if self.enable_printing:
            print(f"{Fore.GREEN}Agent:\n{Style.RESET_ALL}{action}")

    def service(self, response):
        if self.enable_printing:
            print(f"{Fore.BLUE}Service: {Style.RESET_ALL}{response}\n\n")

    def result(self, results):
        print(f"{Fore.MAGENTA}Results:\n{Style.RESET_ALL}{results}")

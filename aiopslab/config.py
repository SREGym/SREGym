# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Abstracts the configuration file for AIOpsLab."""

import yaml


class Config:
    def __init__(self, config_path):
        self.config_path = config_path
        self.config = self._load_config()

    def _load_config(self):
        with open(self.config_path, "r") as file:
            return yaml.safe_load(file)

    def get(self, key, default=None):
        return self.config.get(key, default)


# Usage example
# config = Config(Path("config.yml"))
# data_dir = config.get("data_dir")
# qualitative_eval = config.get("qualitative_eval")

# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import os
import json

from aiopslab.paths import LLM_CACHE_FILE, CACHE_DIR


class LLMCache:
    """A cache for storing the outputs of an LLM."""

    def __init__(self) -> None:
        if os.path.exists(LLM_CACHE_FILE):
            with open(LLM_CACHE_FILE) as f:
                self.cache_dict = json.load(f)
        else:
            os.makedirs(CACHE_DIR, exist_ok=True)
            self.cache_dict = {}

    @staticmethod
    def process_payload(payload):
        if isinstance(payload, (list, dict)):
            return json.dumps(payload)
        return payload

    def get_from_cache(self, payload):
        payload_cache = self.process_payload(payload)
        if payload_cache in self.cache_dict:
            return self.cache_dict[payload_cache]
        return None

    def add_to_cache(self, payload, output):
        payload_cache = self.process_payload(payload)
        self.cache_dict[payload_cache] = output

    def save_cache(self):
        with open(LLM_CACHE_FILE, "w") as f:
            json.dump(self.cache_dict, f, indent=4)

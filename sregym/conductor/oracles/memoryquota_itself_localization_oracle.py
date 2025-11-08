import logging
from typing import Any, override

from kubernetes import client, config
from kubernetes.config.config_exception import ConfigException

from sregym.conductor.oracles.localization_oracle import LocalizationOracle

local_logger = logging.getLogger("all.sregym.localization_oracle")
local_logger.propagate = True
local_logger.setLevel(logging.DEBUG)


class MemoryQuotaItselfLocalizationOracle(LocalizationOracle):

    def __init__(self, problem, namespace: str, expected_memoryquota_name: str):
        super().__init__(problem, namespace)
        self.expected_memoryquota_name = expected_memoryquota_name

    @override
    def expect(self):
        uid = self.memoryquota_uid(self.expected_memoryquota_name, self.namespace)
        return [uid]  # Return only the UID as expected

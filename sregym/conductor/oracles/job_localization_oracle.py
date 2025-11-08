import logging
from typing import Any, override

from kubernetes import client, config
from kubernetes.config.config_exception import ConfigException

from sregym.conductor.oracles.localization_oracle import LocalizationOracle

local_logger = logging.getLogger("all.sregym.localization_oracle")
local_logger.propagate = True
local_logger.setLevel(logging.DEBUG)


class JobLocalizationOracle(LocalizationOracle):

    def __init__(self, problem, namespace: str, expected_job_name: str):
        super().__init__(problem, namespace)
        self.expected_job_name = expected_job_name

    @override
    def expect(self):
        uid = self.job_uid(self.expected_job_name, self.namespace)
        return [uid]  # Return only the UID as expected

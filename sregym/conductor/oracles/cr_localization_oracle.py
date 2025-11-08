import logging
from typing import Any, override

from kubernetes import client, config
from kubernetes.config.config_exception import ConfigException

from sregym.conductor.oracles.localization_oracle import LocalizationOracle

local_logger = logging.getLogger("all.sregym.localization_oracle")
local_logger.propagate = True
local_logger.setLevel(logging.DEBUG)


class CustomResourceLocalizationOracle(LocalizationOracle):

    def __init__(self, problem, namespace: str, resource_type: str, expected_resource_name: str):
        super().__init__(problem, namespace)
        self.resource_type = resource_type
        self.expected_resource_name = expected_resource_name

    @override
    def expect(self):
        uid = self.get_resource_uid(self.resource_type, self.expected_resource_name, self.namespace)
        return [uid]  # Return only the UID as expected

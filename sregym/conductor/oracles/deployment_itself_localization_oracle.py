import logging
from typing import Any, override

from kubernetes import client, config
from kubernetes.config.config_exception import ConfigException

from sregym.conductor.oracles.localization_oracle import LocalizationOracle

local_logger = logging.getLogger("all.sregym.localization_oracle")
local_logger.propagate = True
local_logger.setLevel(logging.DEBUG)


class DeploymentItselfLocalizationOracle(LocalizationOracle):

    def __init__(self, problem, namespace: str, expected_deployment_names: list[str]):
        super().__init__(problem, namespace)
        self.expected_deployment_names = expected_deployment_names

    @override
    def expect(self):
        uids = [
            self.deployment_uid(deployment_name, self.namespace) for deployment_name in self.expected_deployment_names
        ]
        return uids  # Return only the UID as expected

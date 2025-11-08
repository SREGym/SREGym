import logging
from typing import Any, override

from kubernetes import client, config
from kubernetes.config.config_exception import ConfigException

from sregym.conductor.oracles.localization_oracle import LocalizationOracle

local_logger = logging.getLogger("all.sregym.localization_oracle")
local_logger.propagate = True
local_logger.setLevel(logging.DEBUG)


class PodOfDeploymentOracle(LocalizationOracle):

    def __init__(self, problem, namespace: str, expected_deployment_name: str):
        super().__init__(problem, namespace)
        self.expected_deployment_name = expected_deployment_name

    @override
    def expect(self):
        uids, names = self.all_pods_of_deployment_uids(self.expected_deployment_name, self.namespace)
        return uids  # Return only the UID as expected

import logging
from typing import Any, override

from kubernetes import client, config
from kubernetes.config.config_exception import ConfigException

from sregym.conductor.oracles.localization_oracle import LocalizationOracle

local_logger = logging.getLogger("all.sregym.localization_oracle")
local_logger.propagate = True
local_logger.setLevel(logging.DEBUG)


class NetworkPolicyLocalizationOracle(LocalizationOracle):

    def __init__(self, problem, namespace: str, expected_networkpolicy_name: str):
        super().__init__(problem, namespace)
        self.expected_networkpolicy_name = expected_networkpolicy_name

    @override
    def expect(self):
        uid = self.networkpolicy_uid(self.expected_networkpolicy_name, self.namespace)
        return [uid]  # Return only the UID as expected

import logging
from typing import Any, override

from kubernetes import client, config
from kubernetes.config.config_exception import ConfigException

from sregym.conductor.oracles.localization_oracle import LocalizationOracle

local_logger = logging.getLogger("all.sregym.localization_oracle")
local_logger.propagate = True
local_logger.setLevel(logging.DEBUG)


class PodOfDaemonsetOracle(LocalizationOracle):

    def __init__(self, problem, namespace: str, expected_daemonset_name: str):
        super().__init__(problem, namespace)
        self.expected_daemonset_name = expected_daemonset_name

    @override
    def expect(self):
        uids, names = self.all_pods_of_daemonset_uids(self.expected_daemonset_name, self.namespace)
        return uids  # Return only the UID as expected

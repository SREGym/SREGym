import logging
from typing import Any, override

from kubernetes import client, config
from kubernetes.config.config_exception import ConfigException

from sregym.conductor.oracles.localization_oracle import LocalizationOracle

local_logger = logging.getLogger("all.sregym.localization_oracle")
local_logger.propagate = True
local_logger.setLevel(logging.DEBUG)


class OrLocalizationOracle(LocalizationOracle):

    # only for two single localization oracles
    # TODO： implement more complicated oracles

    def __init__(self, problem, namespace: str, oracle1: LocalizationOracle, oracle2: LocalizationOracle):
        super().__init__(problem, namespace)

    @override
    def expect(self):
        oracle1_uid = self.oracle1.expect()
        oracle2_uid = self.oracle2.expect()
        return [oracle1_uid, oracle2_uid]

    @override
    def compare_truth(self, expectation, reality):
        oracle1_expectation = expectation[0]
        oracle2_expectation = expectation[1]
        if len(reality) == 1:
            # one result is acceptable
            return self.oracle1.compare_truth(oracle1_expectation, reality) and self.oracle2.compare_truth(
                oracle2_expectation, reality
            )
        elif len(reality) == 2:
            if type(reality[0]) == list and type(reality[1]) == list:
                # from checkpoint
                return self.oracle1.compare_truth(oracle1_expectation, reality[0]) and self.oracle2.compare_truth(
                    oracle2_expectation, reality[1]
                )
            elif type(reality[0]) == str and type(reality[1]) == str:
                # tolerate the order
                return (
                    self.oracle1.compare_truth(oracle1_expectation, reality[0])
                    and self.oracle2.compare_truth(oracle2_expectation, reality[1])
                    or self.oracle1.compare_truth(oracle1_expectation, reality[1])
                    and self.oracle2.compare_truth(oracle2_expectation, reality[0])
                )
            else:
                raise ValueError(f"Invalid reality format: {reality}")
        else:
            return False  # must be false positive #TODO: support more powerful OR logic.

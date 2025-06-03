# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Inject faults at the hardware layer."""

import json
import yaml

from aiopslab.service.kubectl import KubeCtl
from aiopslab.generators.fault.base import FaultInjector


class HWFaultInjector(FaultInjector):
    def _inject(self, microservices: list[str], fault_type: str):
        return NotImplementedError

    ############# FAULT LIBRARY ################

    # H.1
    def hw_bug(self):
        return NotImplementedError

    ############# HELPER FUNCTIONS ################

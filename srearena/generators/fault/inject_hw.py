"""Inject faults at the hardware layer."""

# TODO: Replace with khaos
import json

import yaml

from srearena.generators.fault.base import FaultInjector
from srearena.service.kubectl import KubeCtl


class HWFaultInjector(FaultInjector):
    def _inject(self, microservices: list[str], fault_type: str):
        return NotImplementedError

    ############# FAULT LIBRARY ################

    # H.1
    def hw_bug(self):
        return NotImplementedError

    ############# HELPER FUNCTIONS ################

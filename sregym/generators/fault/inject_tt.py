import json
import logging
import random
import time
from typing import Any

import yaml

from sregym.generators.fault.base import FaultInjector
from sregym.service.kubectl import KubeCtl

logger = logging.getLogger(__name__)


class TrainTicketFaultInjector(FaultInjector):
    def __init__(self, namespace: str = "train-ticket"):
        super().__init__(namespace)
        self.namespace = namespace
        self.kubectl = KubeCtl()
        self.configmap_name = "flagd-config"
        self.flagd_deployment = "flagd"

        self.supported_faults = {"tt-feat-17", "tt-feat-22"}
        self.excluded_from_decoy = self.supported_faults | {"tt-feat-01"}
        self.all_flags = {
            "tt-feat-01",
            "tt-feat-02",
            "tt-feat-03",
            "tt-feat-04",
            "tt-feat-05",
            "tt-feat-06",
            "tt-feat-07",
            "tt-feat-08",
            "tt-feat-09",
            "tt-feat-10",
            "tt-feat-11",
            "tt-feat-12",
            "tt-feat-13",
            "tt-feat-14",
            "tt-feat-15",
            "tt-feat-16",
            "tt-feat-17",
            "tt-feat-18",
            "tt-feat-19",
            "tt-feat-20",
            "tt-feat-21",
            "tt-feat-22",
        }

    def _get_configmap(self) -> dict[str, Any]:
        try:
            result = self.kubectl.exec_command(
                f"kubectl get configmap {self.configmap_name} -n {self.namespace} -o json"
            )
            return json.loads(result) if result else {}
        except Exception as e:
            logger.error(f"Error getting ConfigMap: {e}")
            return {}

    def _set_fault_state(self, fault_type: str, state: str) -> bool:
        """Update fault state in ConfigMap.

        Args:
            fault_type: Name of the fault (e.g., 'tt-feat-17')
            state: 'on' or 'off'
        """
        if fault_type not in self.supported_faults:
            print(f"Unsupported fault type: {fault_type}")
            return False

        print(f"Setting {fault_type} to {state}...")

        configmap = self._get_configmap()
        if not configmap:
            print("Failed to get ConfigMap")
            return False

        flags_yaml = configmap["data"]["flags.yaml"]
        flags_data = yaml.safe_load(flags_yaml)

        if fault_type not in flags_data["flags"]:
            print(f"Fault '{fault_type}' not found in ConfigMap")
            return False

        flags_data["flags"][fault_type]["defaultVariant"] = state
        updated_yaml = yaml.dump(flags_data, default_flow_style=False)

        try:
            result = self.kubectl.update_configmap(
                name=self.configmap_name, namespace=self.namespace, data={"flags.yaml": updated_yaml}
            )

            if result:
                print(f"✅ {fault_type} set to {state}")

                verification = self._get_configmap()
                if verification and "data" in verification:
                    flags_verification = yaml.safe_load(verification["data"]["flags.yaml"])
                    actual_value = flags_verification["flags"][fault_type]["defaultVariant"]
                    if actual_value == state:
                        print(f"✅ ConfigMap verified: {fault_type} = {state}")
                    else:
                        print(f"❌ ConfigMap verification failed: expected {state}, got {actual_value}")
                        return False

                self._restart_flagd()
                print("✅ flagd restarted successfully")

                print("Sleeping for 20 seconds for flag value change to take effect...")
                time.sleep(20)
                return True
            else:
                print("Failed to update ConfigMap")
                return False

        except Exception as e:
            print(f"❌ Error updating fault: {e}")
            return False

    def _restart_flagd(self):
        print("[TrainTicket] Restarting flagd deployment...")
        try:
            result = self.kubectl.exec_command(
                f"kubectl rollout restart deployment/{self.flagd_deployment} -n {self.namespace}"
            )
            print(f"[TrainTicket] flagd deployment restarted: {result}")
        except Exception as e:
            logger.error(f"Error restarting flagd: {e}")

    def activate_decoy_flags(self, count: int = 3) -> bool:
        """Turn on a random subset of dud flags so the real fault doesn't stand out.

        Args:
            count: How many decoy flags to enable.
        """
        dud_flags = list(self.all_flags - self.excluded_from_decoy)
        random.shuffle(dud_flags)
        chosen = dud_flags[: min(count, len(dud_flags))]

        configmap = self._get_configmap()
        if not configmap:
            print("Failed to get ConfigMap for decoy activation")
            return False

        flags_yaml = configmap["data"]["flags.yaml"]
        flags_data = yaml.safe_load(flags_yaml)

        activated = []
        for flag in chosen:
            if flag in flags_data["flags"]:
                flags_data["flags"][flag]["defaultVariant"] = "on"
                activated.append(flag)

        if not activated:
            print("No decoy flags available in ConfigMap")
            return False

        updated_yaml = yaml.dump(flags_data, default_flow_style=False)
        try:
            result = self.kubectl.update_configmap(
                name=self.configmap_name, namespace=self.namespace, data={"flags.yaml": updated_yaml}
            )
            if result:
                print(f"Decoy flags activated: {activated}")
                return True
            else:
                print("Failed to update ConfigMap with decoy flags")
                return False
        except Exception as e:
            print(f"Error activating decoy flags: {e}")
            return False

    def deactivate_decoy_flags(self) -> bool:
        """Turn off all dud flags (everything except supported faults)."""
        configmap = self._get_configmap()
        if not configmap:
            print("Failed to get ConfigMap for decoy deactivation")
            return False

        flags_yaml = configmap["data"]["flags.yaml"]
        flags_data = yaml.safe_load(flags_yaml)

        deactivated = []
        for flag in self.all_flags - self.excluded_from_decoy:
            if flag in flags_data["flags"] and flags_data["flags"][flag].get("defaultVariant") == "on":
                flags_data["flags"][flag]["defaultVariant"] = "off"
                deactivated.append(flag)

        if not deactivated:
            print("No decoy flags were active")
            return True

        updated_yaml = yaml.dump(flags_data, default_flow_style=False)
        try:
            result = self.kubectl.update_configmap(
                name=self.configmap_name, namespace=self.namespace, data={"flags.yaml": updated_yaml}
            )
            if result:
                print(f"Decoy flags deactivated: {deactivated}")
                return True
            else:
                print("Failed to update ConfigMap for decoy deactivation")
                return False
        except Exception as e:
            print(f"Error deactivating decoy flags: {e}")
            return False

    def _inject(self, fault_type: str, microservices: list[str] | None = None, duration: str | None = None):
        """Override base class _inject to use feature flag-based injection."""
        self.activate_decoy_flags(count=10)
        return self._set_fault_state(fault_type, "on")

    def _recover(self, fault_type: str, microservices: list[str] | None = None):
        """Override base class _recover to use feature flag-based recovery."""
        result = self._set_fault_state(fault_type, "off")
        self.deactivate_decoy_flags()
        return result

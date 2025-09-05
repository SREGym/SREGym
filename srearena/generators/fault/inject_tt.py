import json
import logging
import yaml
from typing import Dict, List, Any, Optional
import time

from srearena.generators.fault.base import FaultInjector
from srearena.service.kubectl import KubeCtl

logger = logging.getLogger(__name__)


class TrainTicketFaultInjector(FaultInjector):

    def __init__(self, namespace: str = "train-ticket"):
        super().__init__(namespace)
        self.namespace = namespace
        self.kubectl = KubeCtl()
        self.configmap_name = "flagd-config"
        self.flagd_deployment = "flagd"

        self.fault_mapping = {
            "fault-1-async-message-sequence-control": "F1: Asynchronous message delivery lacks sequence control",
            "fault-2-data-request-order-inconsistency": "F2: Different data requests for the same report are returned in an unexpected order",
            "fault-3-jvm-docker-config-mismatch": "F3: JVM configurations are inconsistent with Docker configurations",
            "fault-4-ssl-offloading-granularity": "F4: SSL offloading happens in a fine granularity (happening in almost each Docker instance)",
            "fault-5-high-load-timeout-cascade": "F5: The high load of a type of requests causes the timeout failure of another type of requests",
            "fault-6-sql-error-recursive-requests": "F6: Endless recursive requests of a microservice are caused by SQL errors of another dependent microservice",
            "fault-7-third-party-service-overload": "F7: The overload of requests to a third-party service leads to denial of service",
            "fault-8-request-key-propagation-failure": "F8: The key in the request of one microservice is not passed to its dependent microservice",
            "fault-9-css-bidirectional-display-error": "F9: There is a CSS display style error in bi-directional",
            "fault-10-bom-api-unexpected-output": "F10: An API used in a special case of BOM updating returns unexpected output",
            "fault-11-bom-data-sequence-error": "F11: The BOM data is updated in an unexpected sequence",
            "fault-12-price-status-query-chain-error": "F12: Price status querying does not consider an unexpected output of a microservice in its call chain",
            "fault-13-price-optimization-order-error": "F13: Price optimization steps are executed in an unexpected order",
            "fault-14-locked-product-cpi-calculation-error": "F14: There is a mistake in including the locked product in CPI calculation",
            "fault-15-spark-actor-system-config-error": "F15: The spark actor is used for the configuration of actorSystem (part of Apache Spark) instead of the system actor",
            "fault-16-spray-max-content-length-limit": "F16: The 'max-content-length' configuration of spray is only 2 Mb, not allowing to support to upload a big file",
            "fault-17-nested-sql-select-clause-error": "F17: Too many nested 'select' and 'from' clauses are in the constructed SQL statement",
            "fault-18-json-chart-data-null-value": "F18: One key of the returned JSON data for the UI chart includes the null value",
            "fault-19-product-price-french-format-error": "F19: The product price is not formatted correctly in the French format",
            "fault-20-jboss-db2-jar-classpath-error": "F20: The JBoss startup classpath parameter does not include the right DB2 jar package",
            "fault-21-aria-labeled-accessibility-error": "F21: The 'aria-labeled-by' element for accessibility cannot be located by the JAWS",
            "fault-22-sql-column-name-mismatch-error": "F22: The constructed SQL statement includes a wrong column name in the 'select' part according to its 'from' part",
        }

    def inject_fault(self, fault_type: str) -> bool:
        print(f"[TrainTicket] Injecting fault: {fault_type}")
        return self._set_fault_state(fault_type, "on")

    def recover_fault(self, fault_type: str) -> bool:
        print(f"[TrainTicket] Recovering from fault: {fault_type}")
        return self._set_fault_state(fault_type, "off")

    def _get_configmap(self) -> Dict[str, Any]:
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
            fault_type: Name of the fault (e.g., 'fault-6-sql-error-recursive-requests')
            state: 'on' or 'off'
        """
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

        # Update fault state
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

                print("Sleeping for 20 seconds to flag value change to take effect...")
                time.sleep(20)
                return True
            else:
                print("Failed to update ConfigMap")
                return False

        except Exception as e:
            print(f"❌ Error updating fault: {e}")
            return False

    def _restart_flagd(self):
        print(f"[TrainTicket] Restarting flagd deployment...")

        try:
            result = self.kubectl.exec_command(
                f"kubectl rollout restart deployment/{self.flagd_deployment} -n {self.namespace}"
            )
            print(f"[TrainTicket] flagd deployment restarted successfully: {result}")

        except Exception as e:
            logger.error(f"Error restarting flagd: {e}")

    def get_fault_status(self, fault_type: str) -> str:
        try:
            result = self.kubectl.exec_command(
                f"kubectl get configmap {self.configmap_name} -n {self.namespace} -o jsonpath='{{.data.flags\\.yaml}}'"
            )

            if result and fault_type in result:
                import yaml

                flags_data = yaml.safe_load(result)

                if "flags" in flags_data and fault_type in flags_data["flags"]:
                    return flags_data["flags"][fault_type].get("defaultVariant", "unknown")

        except Exception as e:
            logger.error(f"Error getting fault status: {e}")

        return "unknown"

    def list_available_faults(self) -> List[str]:
        return list(self.fault_mapping.keys())

    def get_fault_description(self, fault_name: str) -> Optional[str]:
        return self.fault_mapping.get(fault_name)

    # Override base class methods to use feature flag-based fault injection
    def _inject(self, fault_type: str, microservices: list[str] = None, duration: str = None):
        """Override base class _inject to use feature flag-based injection."""
        print(f"[TrainTicket] Using feature flag injection for: {fault_type}")
        return self.inject_fault(fault_type)

    def _recover(self, fault_type: str, microservices: list[str] = None):
        """Override base class _recover to use feature flag-based recovery."""
        print(f"[TrainTicket] Using feature flag recovery for: {fault_type}")
        return self.recover_fault(fault_type)


if __name__ == "__main__":
    print("TrainTicketFaultInjector - Use via SREArena CLI")

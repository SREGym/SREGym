import json
import subprocess

from sregym.generators.fault.base import FaultInjector
from sregym.service.kubectl import KubeCtl

# Maps feature flags to the deployment(s) that consume them.
# After restarting flagd, these services must also be restarted
# so their OpenFeature SDK reconnects and picks up the new flag value
# immediately instead of falling back to code-level defaults during
# the gRPC EventStream reconnection window.
# Source: https://opentelemetry.io/docs/demo/feature-flags/
_FLAG_TO_DEPLOYMENTS: dict[str, list[str]] = {
    "adFailure": ["ad"],
    "adHighCpu": ["ad"],
    "adManualGc": ["ad"],
    "cartFailure": ["cart"],
    "paymentFailure": ["payment"],
    "paymentUnreachable": ["checkout"],
    "productCatalogFailure": ["product-catalog"],
    "kafkaQueueProblems": ["kafka"],
    "imageSlowLoad": ["frontend"],
    "loadGeneratorFloodHomepage": ["load-generator"],
    "failedReadinessProbe": ["cart"],
    "recommendationCacheFailure": ["recommendation"],
    "emailMemoryLeak": ["email"],
    "llmInaccurateResponse": ["llm"],
    "llmRateLimitError": ["llm"],
}


class OtelFaultInjector(FaultInjector):
    def __init__(self, namespace: str):
        self.namespace = namespace
        self.kubectl = KubeCtl()
        self.configmap_name = "flagd-config"

    def _restart_flagd_and_consumers(self, feature_flag: str) -> None:
        """Restart flagd and the consuming service(s) so the flag change takes effect immediately."""
        self.kubectl.exec_command(f"kubectl rollout restart deployment flagd -n {self.namespace}")

        for deployment in _FLAG_TO_DEPLOYMENTS.get(feature_flag, []):
            self.kubectl.exec_command(f"kubectl rollout restart deployment {deployment} -n {self.namespace}")

        # Wait for flagd to be ready before proceeding
        self.kubectl.exec_command(f"kubectl rollout status deployment flagd -n {self.namespace} --timeout=60s")
        for deployment in _FLAG_TO_DEPLOYMENTS.get(feature_flag, []):
            self.kubectl.exec_command(
                f"kubectl rollout status deployment {deployment} -n {self.namespace} --timeout=120s"
            )

    def inject_fault(self, feature_flag: str):
        command = f"kubectl get configmap {self.configmap_name} -n {self.namespace} -o json"
        try:
            output = self.kubectl.exec_command(command)
            configmap = json.loads(output)
        except subprocess.CalledProcessError as e:
            raise ValueError(f"ConfigMap '{self.configmap_name}' not found in namespace '{self.namespace}'.") from e
        except json.JSONDecodeError as e:
            raise ValueError(f"Error decoding JSON for ConfigMap '{self.configmap_name}'.") from e

        flagd_data = json.loads(configmap["data"]["demo.flagd.json"])

        if feature_flag in flagd_data["flags"]:
            if feature_flag == "imageSlowLoad":
                flagd_data["flags"][feature_flag]["defaultVariant"] = "10sec"
            else:
                flagd_data["flags"][feature_flag]["defaultVariant"] = "on"
        else:
            raise ValueError(f"Feature flag '{feature_flag}' not found in ConfigMap '{self.configmap_name}'.")

        updated_data = {"demo.flagd.json": json.dumps(flagd_data, indent=2)}
        self.kubectl.create_or_update_configmap(self.configmap_name, self.namespace, updated_data)

        self._restart_flagd_and_consumers(feature_flag)

        print(f"Fault injected: Feature flag '{feature_flag}' set to 'on'.")

    def recover_fault(self, feature_flag: str):
        command = f"kubectl get configmap {self.configmap_name} -n {self.namespace} -o json"
        try:
            output = self.kubectl.exec_command(command)
            configmap = json.loads(output)
        except subprocess.CalledProcessError as e:
            raise ValueError(f"ConfigMap '{self.configmap_name}' not found in namespace '{self.namespace}'.") from e
        except json.JSONDecodeError as e:
            raise ValueError(f"Error decoding JSON for ConfigMap '{self.configmap_name}'.") from e

        flagd_data = json.loads(configmap["data"]["demo.flagd.json"])

        if feature_flag in flagd_data["flags"]:
            flagd_data["flags"][feature_flag]["defaultVariant"] = "off"
        else:
            raise ValueError(f"Feature flag '{feature_flag}' not found in ConfigMap '{self.configmap_name}'.")

        updated_data = {"demo.flagd.json": json.dumps(flagd_data, indent=2)}
        self.kubectl.create_or_update_configmap(self.configmap_name, self.namespace, updated_data)

        self._restart_flagd_and_consumers(feature_flag)

        print(f"Fault recovered: Feature flag '{feature_flag}' set to 'off'.")


# Example usage:
# if __name__ == "__main__":
#     namespace = "astronomy-shop"
#     feature_flag = "adServiceFailure"

#     injector = OtelFaultInjector(namespace)

#     injector.inject_fault(feature_flag)
#     injector.recover_fault(feature_flag)

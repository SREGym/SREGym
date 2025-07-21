import json
import yaml
from typing import Optional

from srearena.conductor.oracles.base import Oracle


class MissingCmKeyMitigationOracle(Oracle):

    importance = 1.0

    def __init__(self, problem, configmap_name: str, expected_keys: list[str]):

        super().__init__(problem)
        self.expected_keys = expected_keys
        self.configmap_name = configmap_name

    def evaluate(self) -> dict:
        print("== Missing ConfigMap Key Mitigation Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace

        try:
            cm_yaml = kubectl.exec_command(
                f"kubectl get configmap {self.configmap_name} -n {namespace} -o yaml"
            )
            cm_data = yaml.safe_load(cm_yaml)
            
            config_json_str = cm_data.get("data", {}).get("config.json", "{}")
            config_data = json.loads(config_json_str)
            
            missing_keys = []
            present_keys = []
            
            for key in self.expected_keys:
                if key in config_data:
                    present_keys.append(key)
                else:
                    missing_keys.append(key)
            
            if missing_keys:
                print(f"❌ Missing ConfigMap keys: {missing_keys}")
                return {"success": False}
            else:
                print(f"✅ All expected ConfigMap keys present.")
                return {"success": True}
                    
        except Exception as e:
            print(f"❌ Failed to check ConfigMap: {str(e)}")
            return {"success": False} 
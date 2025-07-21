import json
import yaml
import tempfile
from srearena.conductor.oracles.base import Oracle

class RollingUpdateMitigationOracle(Oracle):
    def __init__(self, problem, deployment_name: str):
        super().__init__(problem)
        self.deployment_name = deployment_name
        self.namespace = problem.namespace
        self.kubectl = problem.kubectl

    def evaluate(self) -> dict:
        print("== Rolling Update Mitigation Evaluation ==")

        try:
            output = self.kubectl.exec_command(
                f"kubectl get deployment {self.deployment_name} -n {self.namespace} -o yaml"
            )
            deployment = yaml.safe_load(output)

            new_init = [ {"name":    "hang-init", "image":   "busybox", "command": ["/bin/sh", "-c", "sleep 15"]}]

            deployment["spec"]["template"]["spec"]["initContainers"] = new_init
            with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as tmp:
                yaml.dump(deployment, tmp)
                tmp_path = tmp.name
            patch_cmd = (
                f"kubectl patch deployment {self.deployment_name} -n {self.namespace} --patch-file {tmp_path}"
            )
            patch_out = self.kubectl.exec_command(patch_cmd)
            print(f"Patched initContainers: {patch_out}")
            
            self.kubectl.wait_for_ready(self.namespace)

            print("🔄 Triggering test rollout…")
            self.kubectl.exec_command(
                f"kubectl rollout restart deployment {self.deployment_name} -n {self.namespace}"
            )

            deploy_json = self.kubectl.exec_command(
                f"kubectl get deployment {self.deployment_name}"
                f" -n {self.namespace} -o json"
            )
            deploy = json.loads(deploy_json)
            avail = deploy["status"].get("availableReplicas", 0)

            if avail > 0:
                print("✅ Mitigation successful: deployment reports availableReplicas >", avail)
                return {"success": True}
            else:
                print("❌ Mitigation failed: No pods available")
                return {"success": False}

        except Exception as e:
            print(f"❌ Error during evaluation: {str(e)}")
            return {"success": False}

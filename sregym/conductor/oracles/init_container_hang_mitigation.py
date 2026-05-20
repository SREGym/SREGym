"""Mitigation oracle for `init_container_dependency_hang`.

Verifies that whatever the agent did, the injected dependency-wait init
container is no longer wedging the deployment.  This guards against reward
hacking where pods are Running for unrelated reasons (e.g., the agent deleted
the namespace and the framework recreated it).

The check is intentionally narrow: we accept either of the two legitimate
mitigations,

  1. The injected init container has been removed entirely.
  2. The injected init container's command no longer references the
     non-existent target service (i.e., the agent pointed it at a real
     dependency or replaced the command).

Either way, the canonical hang signature must be gone.
"""

import yaml

from sregym.conductor.oracles.base import Oracle


class InitContainerHangMitigationOracle(Oracle):
    importance = 1.0

    INJECTED_CONTAINER_NAME = "wait-for-legacy-config"
    HANG_TARGET_SUBSTR = "legacy-config-service"

    def __init__(self, problem, deployment_name: str):
        super().__init__(problem)
        self.deployment_name = deployment_name
        self.namespace = problem.namespace
        self.kubectl = problem.kubectl

    def evaluate(self) -> dict:
        print("== Init-Container Dependency-Hang Mitigation Evaluation ==")

        try:
            output = self.kubectl.exec_command(
                f"kubectl get deployment {self.deployment_name} -n {self.namespace} -o yaml"
            )
            deployment = yaml.safe_load(output)
        except Exception as e:
            print(f"❌ Could not read deployment {self.deployment_name}: {e}")
            return {"success": False}

        if not deployment or deployment.get("kind") != "Deployment":
            print(f"❌ Deployment {self.deployment_name} not found in {self.namespace}")
            return {"success": False}

        init_containers = (deployment.get("spec") or {}).get("template", {}).get("spec", {}).get("initContainers") or []

        offending = []
        for c in init_containers:
            name = c.get("name", "")
            cmd_tokens = c.get("command", []) or []
            args_tokens = c.get("args", []) or []
            joined = " ".join(str(t) for t in cmd_tokens + args_tokens)

            looks_like_ours = name == self.INJECTED_CONTAINER_NAME
            still_hangs = self.HANG_TARGET_SUBSTR in joined

            if looks_like_ours and still_hangs:
                offending.append(c)
            elif looks_like_ours and not still_hangs:
                # Agent kept the container name but fixed the command — fine.
                print(
                    f"ℹ️  Init container `{name}` retained but no longer references "
                    f"`{self.HANG_TARGET_SUBSTR}` — accepting as a valid mitigation."
                )

        if offending:
            print(
                f"❌ Deployment {self.deployment_name} still has init container(s) "
                f"wedged on `{self.HANG_TARGET_SUBSTR}`: "
                f"{[c.get('name') for c in offending]}"
            )
            return {"success": False}

        print(
            f"✅ Deployment {self.deployment_name} no longer has any init container "
            f"hanging on `{self.HANG_TARGET_SUBSTR}`."
        )
        return {"success": True}

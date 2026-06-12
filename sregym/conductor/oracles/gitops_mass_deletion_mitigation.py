"""
Custom mitigation oracle for GitOps mass deletion problem.

Verifies that ALL hotel-reservation deployments have been restored
with at least 1 running replica. Rejects the fix if any deployment
is still scaled to 0 — even if the wrk2-job pod is running.
"""

import time

from sregym.conductor.oracles.base import Oracle

_REQUIRED_DEPLOYMENTS = [
    "consul",
    "frontend",
    "geo",
    "memcached-profile",
    "memcached-rate",
    "memcached-reserve",
    "mongodb-geo",
    "mongodb-profile",
    "mongodb-rate",
    "mongodb-recommendation",
    "mongodb-reservation",
    "mongodb-user",
    "profile",
    "rate",
    "recommendation",
    "reservation",
    "search",
    "user",
]

_ROLLOUT_SETTLE_SECONDS = 60
_ROLLOUT_POLL_INTERVAL = 5


class GitOpsMassDeletionMitigationOracle(Oracle):
    importance = 1.0

    def evaluate(self) -> dict:
        print("== Mitigation Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        results = {}

        # Wait for rollouts to settle
        print("Waiting for deployments to settle...")
        time.sleep(_ROLLOUT_POLL_INTERVAL)

        # Get all deployments in namespace
        deployments = kubectl.list_deployments(namespace)
        deployment_map = {dep.metadata.name: dep for dep in deployments.items}

        all_restored = True

        # Check every required deployment has replicas >= 1
        for dep_name in _REQUIRED_DEPLOYMENTS:
            if dep_name not in deployment_map:
                print(f"❌ Deployment '{dep_name}' not found in namespace")
                all_restored = False
                continue

            dep = deployment_map[dep_name]
            desired = dep.spec.replicas or 0
            ready = dep.status.ready_replicas or 0

            if desired == 0:
                print(f"❌ Deployment '{dep_name}' is still scaled to 0")
                all_restored = False
            elif ready < desired:
                print(f"⚠️  Deployment '{dep_name}' has {ready}/{desired} ready replicas")
                all_restored = False
            else:
                print(f"✅ Deployment '{dep_name}' is healthy ({ready}/{desired})")

        # Also verify the corrupted configmap is gone or fixed
        try:
            result = kubectl.exec_command(
                f"kubectl get configmap cluster-config "
                f"-n {namespace} -o jsonpath='{{.data.template\\.error}}' 2>/dev/null"
            ).strip()
            if result == "true":
                print("❌ Corrupted cluster-config ConfigMap still present with template.error=true")
                all_restored = False
            else:
                print("✅ cluster-config ConfigMap is fixed or removed")
        except Exception:
            print("✅ cluster-config ConfigMap not found (deleted)")

        results["success"] = all_restored

        if all_restored:
            print("\n✅ All deployments restored — mitigation successful!")
        else:
            print("\n❌ Some deployments still missing or scaled to 0")

        return results

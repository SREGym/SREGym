from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.service.rollout import deployment_rollout_complete

OPERATOR_NAMESPACE = "tidb-operator"


class WrongOperatorImageMitigationOracle(MitigationOracle):
    """MitigationOracle's checks of the TiDB cluster, plus the operator itself.

    The fault rolls the operator's pod in ``tidb-operator`` to an image that does
    not exist. MitigationOracle alone checks only the problem namespace,
    ``tidb-cluster``, whose workloads keep running without their operator, so
    it passed with the fault live. The operator's Deployments must also be fully
    rolled out, and its pods running.
    """

    def evaluate(self) -> dict:
        verdict = super().evaluate()
        if not verdict.get("success"):
            return verdict

        kubectl = self.problem.kubectl
        deployments = kubectl.list_deployments(OPERATOR_NAMESPACE).items
        if not deployments:
            print(f"❌ No Deployments in {OPERATOR_NAMESPACE}")
            return self.fail("required_deployment_missing", namespace=OPERATOR_NAMESPACE)
        for dep in deployments:
            if not deployment_rollout_complete(dep):
                ready = getattr(dep.status, "ready_replicas", None) or 0
                desired = dep.spec.replicas if dep.spec.replicas is not None else 1
                print(f"❌ Operator Deployment '{dep.metadata.name}' is not ready ({ready}/{desired})")
                return self.fail(
                    "deployment_replicas_unready",
                    deployment=dep.metadata.name,
                    namespace=OPERATOR_NAMESPACE,
                    ready=ready,
                    desired=desired,
                )

        unready = self.pods_unready(kubectl.list_pods(OPERATOR_NAMESPACE).items, namespace=OPERATOR_NAMESPACE)
        if unready is not None:
            return unready
        return verdict

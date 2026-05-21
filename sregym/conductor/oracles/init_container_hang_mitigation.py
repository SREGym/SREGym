"""Mitigation oracle for `init_container_dependency_hang`.

Verifies that whatever the agent did, the injected dependency-wait init
container is no longer wedging the deployment.  This guards against reward
hacking where pods are Running for unrelated reasons (e.g., the agent deleted
the namespace and the framework recreated it).

What counts as the hang signature
---------------------------------
The hang is a *runtime* state, not a property of the manifest text.  The
injected `wait-for-legacy-config` init container loops on
`nslookup legacy-config-service`; while that loop spins the init container is
not `Completed` and the pod is stuck in `Init:0/1`.  The hang is therefore
resolved by *any* action that lets the init container finish:

  1. The injected init container is removed from the spec entirely, or
  2. The init container's command is repointed at a resolvable target, or
  3. The missing `legacy-config-service` Service is created so the existing
     `nslookup` loop resolves and the init container completes on its own.

All three are legitimate, and case 3 leaves the command string unchanged — so
grepping the deployment's `command`/`args` for `legacy-config-service` would
wrongly reject a perfectly healthy cluster.  Instead we inspect the *live pod
init-container statuses* of the target deployment: success means no pod still
has the injected init container un-completed.
"""

from sregym.conductor.oracles.base import Oracle


class InitContainerHangMitigationOracle(Oracle):
    importance = 1.0

    INJECTED_CONTAINER_NAME = "wait-for-legacy-config"

    def __init__(self, problem, deployment_name: str):
        super().__init__(problem)
        self.deployment_name = deployment_name
        self.namespace = problem.namespace
        self.kubectl = problem.kubectl

    def _owning_deployment(self, pod) -> str:
        """Best-effort deployment name a pod belongs to, via ReplicaSet owner.

        A pod is owned by a ReplicaSet whose name is `<deployment>-<hash>`;
        stripping the trailing hash segment recovers the deployment name.
        """
        for owner in pod.metadata.owner_references or []:
            if owner.kind == "ReplicaSet" and owner.name:
                return owner.name.rsplit("-", 1)[0]
        return ""

    def evaluate(self) -> dict:
        print("== Init-Container Dependency-Hang Mitigation Evaluation ==")

        try:
            pod_list = self.kubectl.list_pods(self.namespace)
        except Exception as e:
            print(f"❌ Could not list pods in {self.namespace}: {e}")
            return {"success": False}

        # Pods of the target deployment that still carry the injected init
        # container.  We match the unique injected container name rather than a
        # label selector so the check is robust to label edits by the agent.
        target_pods = []
        for pod in pod_list.items:
            if self._owning_deployment(pod) != self.deployment_name:
                continue
            init_statuses = (pod.status.init_container_statuses or []) if pod.status else []
            if any(s.name == self.INJECTED_CONTAINER_NAME for s in init_statuses):
                target_pods.append(pod)

        if not target_pods:
            # No live pod still runs the injected init container — either it
            # was removed from the spec, or the deployment fully rolled to a
            # new ReplicaSet without it.  The hang signature is gone.
            print(
                f"✅ No pods of deployment {self.deployment_name} still run the "
                f"injected init container `{self.INJECTED_CONTAINER_NAME}` — "
                f"hang resolved."
            )
            return {"success": True}

        wedged = []
        for pod in target_pods:
            for status in pod.status.init_container_statuses or []:
                if status.name != self.INJECTED_CONTAINER_NAME:
                    continue
                terminated = status.state.terminated if status.state else None
                completed = terminated is not None and terminated.reason == "Completed"
                if not completed:
                    wedged.append(pod.metadata.name)

        if wedged:
            print(
                f"❌ Deployment {self.deployment_name} still has pod(s) with the "
                f"injected init container `{self.INJECTED_CONTAINER_NAME}` not "
                f"completed: {wedged}"
            )
            return {"success": False}

        # The injected init container is still present in the spec, but every
        # pod's copy ran to completion — the agent made the dependency
        # resolvable (created the Service or repointed the command).
        print(
            f"✅ Injected init container `{self.INJECTED_CONTAINER_NAME}` ran to "
            f"completion on all pods of deployment {self.deployment_name} — "
            f"hang resolved."
        )
        return {"success": True}

"""Mitigation oracle for `init_container_dependency_hang`.

Verifies that whatever the agent did, the injected dependency-wait init
container can no longer wedge the deployment.  This guards against reward
hacking where the canonical hang signature is *absent* for reasons unrelated
to actually fixing the init container.

Why this is not just `MitigationOracle`
---------------------------------------
The generic `MitigationOracle` checks "every pod in the namespace is
`Running`".  That catches the honest cases (init container removed, or it ran
to completion), but it has no notion of *which deployment* a pod belongs to,
so it is blind to a reward hack: an agent can delete the wedged pod — or
delete the namespace and let the framework recreate a clean app — leaving the
broken init-container spec fully intact.  At evaluation time every *remaining*
pod is `Running`, `MitigationOracle` passes, yet the next pod the deployment
schedules would wedge all over again.  Nothing was fixed.

This oracle closes that gap by anchoring on the *deployment spec*, which
survives pod churn:

  * Injected init container no longer in the spec  -> genuinely removed -> ok.
  * Injected init container still in the spec      -> only legitimate if the
    dependency is now resolvable, proven by either:
      - the `legacy-config-service` Service now existing in the namespace
        (the agent created the missing dependency), or
      - the deployment's current pods having run the injected init container
        to `Completed` (the agent repointed the command at a resolvable
        target).
    If the broken init container is still in the spec with no resolvable
    dependency, the hang is merely dormant -> fail.

The oracle waits for the target deployment's rollout to settle before
reading state, so it is correct when run on its own and does not rely on a
sibling oracle in the CompoundedOracle running first.
"""

import time

from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.base import Oracle

# Time to let the target deployment finish rolling out before we read pod
# state, so a repointed init container that is still legitimately `Init:0/1`
# mid-rollout is not mistaken for a dormant hang.  Mirrors the constants in
# `MitigationOracle`; the oracle waits independently so it is correct even if
# run on its own rather than inside the CompoundedOracle.
_ROLLOUT_SETTLE_SECONDS = 60
_ROLLOUT_POLL_INTERVAL = 5


class InitContainerHangMitigationOracle(Oracle):
    importance = 1.0

    INJECTED_CONTAINER_NAME = "wait-for-legacy-config"
    HANG_TARGET_SERVICE = "legacy-config-service"

    def __init__(self, problem, deployment_name: str):
        super().__init__(problem)
        self.deployment_name = deployment_name
        self.namespace = problem.namespace
        self.kubectl = problem.kubectl

    def _deployment_replicaset_names(self) -> set:
        """Names of all ReplicaSets owned by the target deployment.

        Resolved via `ownerReferences` (ReplicaSet -> Deployment) rather than
        by string-munging pod/RS names, so a pod is attributed to the
        deployment exactly — no false matches from name prefixes.
        """
        replicasets = self.kubectl.get_matching_replicasets(self.namespace, self.deployment_name)
        return {rs.metadata.name for rs in replicasets if rs.metadata and rs.metadata.name}

    def _pod_belongs_to_deployment(self, pod, replicaset_names: set) -> bool:
        """True if the pod is owned by one of the deployment's ReplicaSets."""
        for owner in (pod.metadata.owner_references or []) if pod.metadata else []:
            if owner.kind == "ReplicaSet" and owner.name in replicaset_names:
                return True
        return False

    def _wait_for_deployment_rollout(self) -> None:
        """Poll the target deployment until its rollout finishes (or time out).

        Without this, `evaluate()` could read pod state mid-rollout — a
        repointed init container still in `Init:0/1` would be misjudged a
        dormant hang.  Best-effort: on timeout we proceed and evaluate the
        current state rather than block forever.
        """
        deadline = time.monotonic() + _ROLLOUT_SETTLE_SECONDS
        while time.monotonic() < deadline:
            try:
                dep = self.kubectl.get_deployment(self.deployment_name, self.namespace)
            except Exception:
                # Transient read error — retry until the deadline.
                time.sleep(_ROLLOUT_POLL_INTERVAL)
                continue
            status = dep.status
            spec = dep.spec
            desired = (spec.replicas if spec and spec.replicas is not None else 1) if spec else 1
            settled = (
                status is not None
                and (status.updated_replicas or 0) >= desired
                and (status.ready_replicas or 0) >= desired
                and (status.unavailable_replicas or 0) == 0
            )
            if settled:
                return
            time.sleep(_ROLLOUT_POLL_INTERVAL)
        print(f"⚠️  Timed out waiting for deployment {self.deployment_name} to settle; evaluating current state.")

    def _injected_init_container_in_spec(self, deployment) -> bool:
        """True if the injected init container is still in the deployment's pod
        template — i.e. every pod the deployment schedules would carry it."""
        spec = deployment.spec
        pod_spec = spec.template.spec if spec and spec.template else None
        init_containers = (pod_spec.init_containers or []) if pod_spec else []
        return any(c.name == self.INJECTED_CONTAINER_NAME for c in init_containers)

    def _hang_target_service_exists(self) -> bool:
        """True if the dependency Service the init container waits on now
        exists — the agent created the missing dependency."""
        try:
            services = self.kubectl.list_services(self.namespace)
        except Exception as e:
            # Fail safe: if we cannot confirm the Service exists, do not let
            # that masquerade as a resolvable dependency.
            print(f"⚠️  Could not list services in {self.namespace}: {e}")
            return False
        return any(svc.metadata.name == self.HANG_TARGET_SERVICE for svc in (services.items or []))

    def _injected_init_completed_on_live_pods(self) -> bool:
        """True if the deployment currently has pods whose injected init
        container ran to `Completed` — the agent made the dependency
        resolvable (e.g. repointed the command at a real service).

        Requires at least one live pod actually carrying the injected init
        container; an empty result is *not* treated as completion, so deleting
        the wedged pod cannot masquerade as a fix.
        """
        try:
            pod_list = self.kubectl.list_pods(self.namespace)
            replicaset_names = self._deployment_replicaset_names()
        except Exception as e:
            # Fail safe: if we cannot read pods/replicasets, do not treat that
            # as proof the init container completed.
            print(f"⚠️  Could not list pods/replicasets in {self.namespace}: {e}")
            return False

        saw_injected = False
        for pod in pod_list.items:
            if not self._pod_belongs_to_deployment(pod, replicaset_names):
                continue
            init_statuses = (pod.status.init_container_statuses or []) if pod.status else []
            for status in init_statuses:
                if status.name != self.INJECTED_CONTAINER_NAME:
                    continue
                saw_injected = True
                terminated = status.state.terminated if status.state else None
                if terminated is None or terminated.reason != "Completed":
                    # A live pod still has the injected init container not
                    # completed — definitely not a clean mitigation.
                    return False

        return saw_injected

    def evaluate(self) -> dict:
        print("== Init-Container Dependency-Hang Mitigation Evaluation ==")

        # Let any in-progress rollout finish first, so we evaluate a stable
        # spec/pod state rather than a transient mid-rollout window.
        self._wait_for_deployment_rollout()

        try:
            deployment = self.kubectl.get_deployment(self.deployment_name, self.namespace)
        except ApiException as e:
            if e.status == 404:
                print(f"❌ Deployment {self.deployment_name} not found in {self.namespace}")
            else:
                print(f"❌ Could not read deployment {self.deployment_name}: {e}")
            return {"success": False}
        except Exception as e:
            print(f"❌ Could not read deployment {self.deployment_name}: {e}")
            return {"success": False}

        # Case 1: the injected init container is gone from the spec entirely.
        # Every future pod is clean — genuine removal.
        if not self._injected_init_container_in_spec(deployment):
            print(
                f"✅ Deployment {self.deployment_name} no longer declares the injected "
                f"init container `{self.INJECTED_CONTAINER_NAME}` — hang resolved."
            )
            return {"success": True}

        # Case 2: the init container is still in the spec.  This is only a
        # legitimate mitigation if the dependency it waits on is now
        # resolvable; otherwise the hang is just dormant and the next pod the
        # deployment schedules will wedge again.
        if self._hang_target_service_exists():
            print(
                f"✅ Injected init container `{self.INJECTED_CONTAINER_NAME}` is still in "
                f"the spec, but Service `{self.HANG_TARGET_SERVICE}` now exists — its "
                f"nslookup loop resolves and the init container completes. Hang resolved."
            )
            return {"success": True}

        if self._injected_init_completed_on_live_pods():
            print(
                f"✅ Injected init container `{self.INJECTED_CONTAINER_NAME}` is still in "
                f"the spec, but ran to completion on the deployment's current pods — the "
                f"agent repointed it at a resolvable target. Hang resolved."
            )
            return {"success": True}

        print(
            f"❌ Deployment {self.deployment_name} still declares the injected init "
            f"container `{self.INJECTED_CONTAINER_NAME}` and its dependency "
            f"`{self.HANG_TARGET_SERVICE}` is not resolvable — the hang is only "
            f"dormant and will recur on the next pod. Not a valid mitigation."
        )
        return {"success": False}

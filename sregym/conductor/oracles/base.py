"""Base class for evaluation oracles."""

from abc import ABC, abstractmethod

from sregym.conductor.oracles.failure import classify


class Oracle(ABC):
    #: Reason codes this oracle uses that the shared table does not cover, or
    #: covers differently. Consulted before ``SHARED_FAILURE_CLASSES``, so a
    #: subclass can take its own view of a shared reason without editing the
    #: shared table. Subclasses that only use shared reasons leave this empty.
    #:
    #: Tables merge along the MRO rather than shadowing -- see
    #: ``_failure_classes`` -- so declaring one here is safe in a subclass of an
    #: oracle that already has one.
    FAILURE_CLASSES: dict[str, str] = {}

    def __init__(self, problem):
        self.problem = problem

    def capture_baseline(self) -> None:
        """Record the healthy pre-fault cluster state.

        Called once the app is deployed and before the fault is injected.
        Oracles cannot do this in __init__: the Problem is constructed before
        deploy_app(), when the namespace does not exist yet. Defaults to a
        no-op for oracles that need no baseline.
        """
        return

    @classmethod
    def _failure_classes(cls) -> dict[str, str]:
        """This oracle's table merged with every table it inherits.

        A plain ``cls.FAILURE_CLASSES`` lookup finds only the nearest table in
        the MRO, so a subclass that declares one *shadows* its parent's
        entirely. That is a quiet trap here: the oracles that subclass
        ``MitigationOracle`` run its generic Deployment checks through
        ``super().evaluate()``, and those call ``self.fail`` -- so declaring a
        table for one local reason would silently drop the parent's
        baseline-backed view of a deleted Deployment back to ambiguous.

        Merging in reverse MRO order keeps subclass entries winning, which is
        what an override is for, while inheriting the rest.
        """
        merged: dict[str, str] = {}
        for klass in reversed(cls.__mro__):
            merged.update(vars(klass).get("FAILURE_CLASSES", {}))
        return merged

    @classmethod
    def fail(cls, reason: str, **detail) -> dict:
        """Build a failure verdict that says why, and whose fault it was.

        ``reason`` is a stable snake_case code and is the part consumers may
        filter and aggregate on -- it reaches the results CSV as
        ``Mitigation.reason``. ``detail`` is free-form and diagnostic rather
        than a contract: consumers must tolerate it being absent or differently
        shaped between oracles.

        This is additive to the ``print`` an oracle already emits. The two have
        different consumers and neither replaces the other: the print goes to
        the run log and is what someone reads when a single run looks wrong, and
        it can carry detail too unwieldy for a column. No existing print should
        be removed in favour of a reason code.
        """
        verdict = {
            "success": False,
            "reason": reason,
            "failure_class": classify(reason, cls._failure_classes()),
        }
        if detail:
            verdict["detail"] = detail
        return verdict

    @classmethod
    def fail_from_exception(cls, exc: BaseException, **detail) -> dict:
        """Build a failure verdict from an exception an oracle caught itself.

        Several oracles wrap their whole evaluation in ``except Exception`` and
        return a bare failure. That collapses two different things: the API
        server being unreachable or refusing us (the cluster's problem, and
        common) and a genuine bug in the oracle (ours, and rare). The exception
        type separates them for free, so there is no reason to keep guessing.

        Note this is *not* the same path as the conductor's handler, which sees
        exceptions that escaped an oracle entirely. Here the oracle chose to
        catch, so it still owes a verdict.
        """
        # Imported lazily: ``base`` is imported by every oracle, and the
        # kubernetes client is slow to import and not needed to classify.
        from kubernetes.client.rest import ApiException

        if isinstance(exc, ApiException):
            return cls.fail(
                "kubernetes_api_error",
                status=getattr(exc, "status", None),
                error=f"{type(exc).__name__}: {exc}",
                **detail,
            )
        return cls.fail("oracle_raised", error=f"{type(exc).__name__}: {exc}", **detail)

    def pods_unready(self, pods, **detail) -> dict | None:
        """Return a verdict for the first unhealthy pod or container, else None.

        *pods* is an iterable of pod objects -- pass ``pod_list.items``, or an
        already-filtered list where the oracle selected pods by label.

        Several oracles walked pods with near-identical logic and their own bare
        ``success: False``. Sharing the walk keeps the *classification* in one
        place, which is the part that drifts: the same symptom reported as four
        different reasons would be worse than not reporting it.

        Always ``pods_not_ready`` and therefore ambiguous. A pod that will not
        run may be the agent's collateral damage or a node that cannot host it,
        and nothing observable here separates those.
        """
        for pod in pods:
            pod_name = pod.metadata.name
            if pod.status.phase != "Running":
                print(f"❌ Pod {pod_name} is in phase: {pod.status.phase}")
                return self.fail("pods_not_ready", pod=pod_name, phase=pod.status.phase, **detail)

            for container_status in pod.status.container_statuses or []:
                name = container_status.name
                state = container_status.state
                if state.waiting and state.waiting.reason:
                    print(f"❌ Container {name} is waiting: {state.waiting.reason}")
                    return self.fail(
                        "pods_not_ready", pod=pod_name, container=name, waiting=state.waiting.reason, **detail
                    )
                if state.terminated and state.terminated.reason != "Completed":
                    print(f"❌ Container {name} terminated: {state.terminated.reason}")
                    return self.fail(
                        "pods_not_ready", pod=pod_name, container=name, terminated=state.terminated.reason, **detail
                    )
                if not container_status.ready:
                    print(f"❌ Container {name} is not ready")
                    return self.fail("pods_not_ready", pod=pod_name, container=name, **detail)
        return None

    @abstractmethod
    def evaluate(self, solution, trace, duration) -> dict:
        """Evaluate a solution."""
        pass

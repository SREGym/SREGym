"""Slack Spine: images."""

from __future__ import annotations

import json

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.incorrect_image_mitigation import IncorrectImageMitigationOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.problems.apps.slack_spine.helpers import (
    fetch_parallel,
    role_pod_ips,
    wait_rollout,
)


class _CorrelatedImageOracle(IncorrectImageMitigationOracle):
    """IncorrectImageMitigationOracle whose bad image is resolved at injection time."""

    def __init__(self, problem):
        super().__init__(problem, actual_images={})

    def evaluate(self) -> dict:
        self.actual_images = {service: self.problem.bad_image for service in self.problem.faulty_service}
        return super().evaluate()


class _StatefulSetImageOracle(Oracle):
    """The StatefulSets no longer run the injected image, are fully rolled out, and every pod is Ready."""

    importance = 1.0

    def __init__(self, problem):
        super().__init__(problem)
        self.apps = MitigationOracle(problem)

    def capture_baseline(self) -> None:
        self.apps.capture_baseline()

    def evaluate(self) -> dict:
        print("== Mitigation Evaluation ==")
        api = self.problem.kubectl.apps_v1_api
        still_wrong, unready = {}, {}
        for name in self.problem.faulty_service:
            sts = api.read_namespaced_stateful_set(name, self.problem.namespace)
            image = sts.spec.template.spec.containers[0].image
            if image == self.problem.BAD_IMAGE:
                print(f"❌ StatefulSet {name} still uses {image}")
                still_wrong[name] = image
                continue
            status = sts.status
            desired = sts.spec.replicas if sts.spec.replicas is not None else 1
            if (
                (status.ready_replicas or 0) < desired
                or (status.updated_replicas or 0) < desired
                or (status.update_revision and status.current_revision != status.update_revision)
            ):
                print(f"❌ StatefulSet {name} rollout incomplete ({status.ready_replicas or 0}/{desired} ready)")
                unready[name] = status.ready_replicas or 0
            else:
                print(f"✅ StatefulSet {name} runs {image} and is Ready")
        if still_wrong:
            return self.fail("fault_still_present", statefulsets=still_wrong)
        if unready:
            return self.fail("statefulset_unready", statefulsets=unready)
        return self.apps.evaluate()


class StoreStrictEventOracle(Oracle):
    """The injected delay is gone from the role and its user endpoint answers promptly.

    Clearing the runtime toggle is the direct fix; leaving it active is also
    accepted when the role's effective ``STORE_HOLD_MS`` was cut to a sane
    dose (at most ``MAX_HOLD_MS`` and below the injected one), in which case
    the latency probe decides.
    """

    importance = 1.0
    PROBES = 5
    MAX_LATENCY_S = 2.0
    MAX_HOLD_MS = 1000.0
    DEFAULT_HOLD_MS = 250.0  # servicekit's default when the env is unset
    FAILURE_CLASSES = {
        "endpoint_slow_or_failing": FailureClass.AGENT_ERROR,
        "probe_unavailable": FailureClass.AMBIGUOUS,
    }

    def evaluate(self, *args, **kwargs) -> dict:
        print("== Runtime toggle mitigation check ==")
        p = self.problem
        try:
            if not wait_rollout(p, p.faulty_service, timeout_s=180):
                return self.fail("required_deployment_not_rolled_out", deployment=p.faulty_service)
            hold_ms = self._effective_hold_ms()
            hold_ok = hold_ms <= self.MAX_HOLD_MS and hold_ms < float(p.HOLD_MS)
            pods = role_pod_ips(p, p.role)
            events = fetch_parallel(p, [(n, f"http://{ip}:8000/admin/event", "GET", None) for n, ip in pods.items()])
            for pod, (status, body, _) in events.items():
                active = json.loads(body).get("active", []) if int(status) == 200 else None
                print(f"[live] {pod} active events={active}")
                if active is None or (p.event in active and not hold_ok):
                    return self.fail("fault_still_present", pod=pod, active=active, store_hold_ms=hold_ms)
                if p.event in active:
                    print(f"[live] {pod}: {p.event} still active, but STORE_HOLD_MS={hold_ms:g}; probing latency")
            # One at a time: a concurrent burst would mostly measure the toolbox's own CPU throttling.
            probes = {}
            for i in range(self.PROBES):
                url = f"http://{p.faulty_service}:8000/unread?user_id=probe-{i}&channel_id=probe"
                probes.update(fetch_parallel(p, [(f"unread-{i}", url, "GET", None)]))
        except Exception as exc:
            print(f"❌ Probe failed: {exc}")
            return self.fail("probe_unavailable", error=str(exc))
        slow = {k: (s, round(t, 2)) for k, (s, _, t) in probes.items() if int(s) != 200 or t > self.MAX_LATENCY_S}
        print(f"[probe] GET /unread: {len(probes) - len(slow)}/{len(probes)} fast and OK")
        if slow:
            return self.fail("endpoint_slow_or_failing", slow=slow)
        print("✅ Injected delay removed and /unread is fast")
        return {"success": True}

    def _effective_hold_ms(self) -> float:
        """``STORE_HOLD_MS`` of the role's (rolled-out) app container."""
        p = self.problem
        dep = p.kubectl.get_deployment(p.faulty_service, p.namespace)
        container = next((c for c in dep.spec.template.spec.containers if c.name == "app"), None)
        container = container or dep.spec.template.spec.containers[0]
        env = {item.name: item.value for item in container.env or [] if item.value is not None}
        try:
            return float(env.get("STORE_HOLD_MS", self.DEFAULT_HOLD_MS))
        except ValueError:
            return float("inf")

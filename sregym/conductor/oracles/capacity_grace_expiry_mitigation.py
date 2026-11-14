"""Mitigation oracle for CapacityGraceExpiry."""

import re
import shlex
import time

from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.mitigation import MitigationOracle

_LOGIN_URL = "http://frontend:5000/user?username=Cornell_1&password=1111111111"
_LOGIN_OK = "Login successfully!"


class CapacityGraceExpiryMitigationOracle(MitigationOracle):
    """Logins must work, and keep working across several controller syncs.

    The grading is behavioural so that every durable fix passes: correcting the
    usage record, disabling enforcement, or stopping/removing the controller.
    Scaling the service back up without doing one of those is undone on the
    next sync and fails.
    """

    importance = 1.0

    FAILURE_CLASSES = {
        # Restoring replicas while the controller still acts on the wrong usage
        # record is the temporary patch this problem exists to tell apart.
        "fix_reverted_by_automation": FailureClass.AGENT_ERROR,
        # Pushing the start date out, or slowing the sync, hides the same cut
        # behind a new grace period.
        "enforcement_only_deferred": FailureClass.AGENT_ERROR,
        "login_failing": FailureClass.AGENT_ERROR,
        "http_probe_response_missing": FailureClass.AMBIGUOUS,
        "no_probe_pod_available": FailureClass.HARNESS_ERROR,
    }

    # How long logins get to come back once the service has ready pods.
    settle_seconds = 60
    # Number of controller syncs the fix has to survive.
    observed_syncs = 4
    poll_seconds = 5

    def evaluate(self) -> dict:
        print("== Capacity Grace Expiry Mitigation Evaluation ==")
        results = super().evaluate()
        if not results.get("success"):
            return results
        try:
            return self._evaluate_durable()
        except Exception as exc:
            print(f"[FAIL] Error while checking login durability: {exc}")
            return self.fail_from_exception(exc)

    def _evaluate_durable(self) -> dict:
        service = self.problem.faulty_service

        if not self._target_serving():
            print(f"❌ deployment/{service} has no ready replicas")
            return self.fail("fault_still_present", deployment=service)

        deferred = self._deferred_only()
        if deferred:
            print(f"❌ Enforcement on the unchanged usage record is only postponed ({deferred})")
            return self.fail("enforcement_only_deferred", deployment=service, change=deferred)

        failure = self._wait_for_login()
        if failure is not None:
            return failure

        window = self.observed_syncs * self.problem.sync_interval_seconds + self.poll_seconds
        deadline = time.monotonic() + window
        login_failures = 0
        while True:
            if not self._target_serving():
                print(f"❌ deployment/{service} lost its replicas again during the observation window")
                return self.fail(
                    "fix_reverted_by_automation", controller=self.problem.controller_name, deployment=service
                )
            ok, detail = self._login()
            if ok is None:
                return detail
            if not ok:
                login_failures += 1
                if login_failures > 1:
                    print("❌ Logins through the frontend are failing")
                    return self.fail("login_failing", output=detail)
            if time.monotonic() >= deadline:
                break
            time.sleep(self.poll_seconds)

        print(f"✅ Logins succeed and deployment/{service} stayed up for {window}s")
        return {"success": True}

    def _target_serving(self) -> bool:
        dep = self.problem.kubectl.get_deployment(self.problem.faulty_service, self.problem.namespace)
        return (dep.spec.replicas or 0) >= 1 and (dep.status.ready_replicas or 0) >= 1

    def _deferred_only(self) -> str | None:
        """Name the postponement if the controller is still armed on the wrong record.

        Only the two edits that outlast the observation window are checked: a
        later start and a slower sync. Anything else shows up as behaviour.
        """
        problem = self.problem
        injected_start = getattr(problem, "_effective_from", None)
        if injected_start is None:
            return None
        core, apps = problem.kubectl.core_v1_api, problem.kubectl.apps_v1_api
        try:
            controller = apps.read_namespaced_deployment(problem.controller_name, problem.controller_namespace)
            usage = core.read_namespaced_config_map(problem.usage_configmap, problem.controller_namespace).data or {}
            policy = core.read_namespaced_config_map(problem.policy_configmap, problem.controller_namespace).data or {}
        except ApiException as e:
            if e.status == 404:
                return None
            raise

        if (controller.spec.replicas or 0) < 1 or policy.get("enforcement") != "enabled":
            return None
        recorded = usage.get(problem.faulty_service, "")
        if not recorded.isdigit() or int(recorded) >= 1:
            return None

        start = policy.get("effective_from", "")
        if start.isdigit() and int(start) > injected_start:
            return "effective_from moved later"
        interval = policy.get("sync_interval_seconds", "")
        if interval.isdigit() and int(interval) > problem.sync_interval_seconds:
            return "sync_interval_seconds raised"
        return None

    def _wait_for_login(self) -> dict | None:
        deadline = time.monotonic() + self.settle_seconds
        while True:
            ok, detail = self._login()
            if ok:
                return None
            if ok is None:
                return detail
            if time.monotonic() >= deadline:
                print("❌ Logins through the frontend are failing")
                return self.fail("login_failing", output=detail)
            time.sleep(self.poll_seconds)

    def _probe_pod(self) -> str | None:
        pods = self.problem.kubectl.list_pods(self.problem.namespace).items
        running = [p.metadata.name for p in pods if p.status.phase == "Running" and p.metadata.name]
        # consul is always present in hotel-reservation and ships wget.
        for name in running:
            if name.startswith("consul"):
                return name
        for name in running:
            if "frontend" not in name and "wrk2" not in name:
                return name
        return None

    def _login(self):
        """Attempt one real login. Returns (True|False, output) or (None, failure verdict)."""
        namespace = self.problem.namespace
        pod = self._probe_pod()
        if not pod:
            print("❌ No suitable probe pod found")
            return None, self.fail("no_probe_pod_available", namespace=namespace)
        # wget exits nonzero on HTTP errors; keep its output rather than masking
        # a failure to run the command through Kubernetes.
        script = f"wget -T 10 -S -O - {shlex.quote(_LOGIN_URL)} 2>&1 || true"
        cmd = f"kubectl exec {shlex.quote(pod)} -n {shlex.quote(namespace)} -- sh -c {shlex.quote(script)}"
        output = self.problem.kubectl.exec_command_checked(cmd, timeout=30)
        statuses = re.findall(r"^\s*HTTP/\S+\s+(\d{3})\b", output, re.MULTILINE)
        if not statuses:
            return None, self.fail("http_probe_response_missing", pod=pod, output=output.strip()[:500])
        return statuses[-1] == "200" and _LOGIN_OK in output, output.strip()[-300:]

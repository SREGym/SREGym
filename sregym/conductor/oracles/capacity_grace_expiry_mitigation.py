"""Mitigation oracle for CapacityGraceExpiry."""

import re
import time

from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.http_probe import find_probe_pod, wget_from_pod
from sregym.conductor.oracles.mitigation import MitigationOracle

_LOGIN_URL = "http://frontend:5000/user?username=Cornell_1&password=1111111111"
_LOGIN_OK = "Login successfully!"

# A probe that could not run at all, as opposed to one that ran and got no answer.
_PROBE_TOOLING_MISSING = re.compile(r"not found|no such file or directory", re.IGNORECASE)

_MISSING = "<no value>"  # what go-template prints for an absent key
_POLICY_KEYS = ("enforcement", "effective_from", "sync_interval_seconds")
_WHOLE_NUMBER = re.compile(r"[0-9]+")


def _fields(text: str) -> list[str]:
    """Split the way the controller's shell and awk do: on runs of space, tab and newline."""
    return [field for field in re.split(r"[ \t\n]+", text) if field]


def _whole(field: str | None) -> int | None:
    return int(field) if field is not None and _WHOLE_NUMBER.fullmatch(field) else None


def read_policy(data: dict | None) -> tuple[bool, int | None, int | None]:
    """Read the policy exactly as the controller script does: (enforcing, start, interval).

    The script prints the three values on one line and takes the first three
    whitespace-separated fields, so padding is ignored and a value containing
    a space shifts the fields after it. Grading has to see what the controller
    sees, or an edit could mean one thing to it and another to the oracle.
    """
    data = data or {}
    fields = _fields(" ".join(data.get(key, _MISSING) for key in _POLICY_KEYS))
    mode, start, interval = (fields + [None, None, None])[:3]
    start = _whole(start)
    return mode == "enabled" and start is not None, start, _whole(interval)


def read_usage(data: dict | None, name: str) -> int | None:
    """The cap the controller applies to ``name``, or None when it applies none."""
    caps: dict[str, str | None] = {}
    for key, value in (data or {}).items():
        for line in f"{key} {value}".split("\n"):
            fields = _fields(line)
            if fields:
                caps[fields[0]] = fields[1] if len(fields) > 1 else None
    return _whole(caps.get(name))


class CapacityGraceExpiryMitigationOracle(MitigationOracle):
    """Logins must work, and keep working while the controller goes on reconciling.

    The grading is behavioural so that every durable fix passes: correcting the
    usage record, disabling enforcement, or stopping/removing the controller.
    Scaling the service back up without doing one of those is undone on the
    next sync and fails.
    """

    importance = 1.0
    evaluation_timeout_seconds = 600

    FAILURE_CLASSES = {
        # Restoring replicas while the controller still acts on the wrong usage
        # record is the temporary patch this problem exists to tell apart.
        "fix_reverted_by_automation": FailureClass.AGENT_ERROR,
        # Pushing the start date out, or slowing the sync, hides the same cut
        # behind a new grace period.
        "enforcement_only_deferred": FailureClass.AGENT_ERROR,
        "login_failing": FailureClass.AGENT_ERROR,
        # The controller is still enabled but did not complete the syncs we wait
        # for, so the fix was never put to the test. That is true of a controller
        # parked in a long sleep and of one rewritten to do nothing; we cannot
        # tell which.
        "reconciliation_not_observed": FailureClass.AMBIGUOUS,
        "http_probe_response_missing": FailureClass.AMBIGUOUS,
        "no_probe_pod_available": FailureClass.HARNESS_ERROR,
    }

    # How long logins get to come back once the service has ready pods.
    settle_seconds = 60
    # Completed controller syncs the fix has to survive.
    observed_syncs = 4
    poll_seconds = 5
    # How many times the expected duration an enabled controller gets to complete those syncs.
    sync_patience = 3

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
        started = time.monotonic()
        syncs_seen = 0
        last_progress = self._sync_progress()
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

            progress = self._sync_progress()
            if progress is not None and progress != last_progress:
                syncs_seen += 1
            last_progress = progress
            # Elapsed time proves nothing about a controller that is asleep, so
            # one that is still enabled has to be seen reconciling. Its progress
            # record being gone leaves only the clock to go on.
            awaiting_syncs = progress is not None and syncs_seen < self.observed_syncs and self._controller_enabled()

            elapsed = time.monotonic() - started
            # A tolerated failed login has to be followed by one that works.
            if elapsed >= window and ok and not awaiting_syncs:
                break
            if elapsed >= window * self.sync_patience and awaiting_syncs:
                print(
                    f"❌ {self.problem.controller_name} is enabled but completed {syncs_seen} sync(s) in {elapsed:.0f}s"
                )
                return self.fail(
                    "reconciliation_not_observed",
                    controller=self.problem.controller_name,
                    syncs_seen=syncs_seen,
                    syncs_required=self.observed_syncs,
                )
            time.sleep(self.poll_seconds)

        print(f"✅ Logins succeed and deployment/{service} stayed up for {elapsed:.0f}s ({syncs_seen} sync(s) seen)")
        return {"success": True}

    def _target_serving(self) -> bool:
        dep = self.problem.kubectl.get_deployment(self.problem.faulty_service, self.problem.namespace)
        return (dep.spec.replicas or 0) >= 1 and (dep.status.ready_replicas or 0) >= 1

    def _read(self, read, name: str):
        """Read an object from the controller's namespace; None once it is gone."""
        try:
            return read(name, self.problem.controller_namespace)
        except ApiException as e:
            if e.status == 404:
                return None
            raise

    def _controller_config(self) -> tuple[bool, int | None, int | None]:
        """(enforcing, start, interval) for a controller that is running; not enforcing otherwise."""
        problem = self.problem
        controller = self._read(problem.kubectl.apps_v1_api.read_namespaced_deployment, problem.controller_name)
        policy = self._read(problem.kubectl.core_v1_api.read_namespaced_config_map, problem.policy_configmap)
        if controller is None or policy is None or (controller.spec.replicas or 0) < 1:
            return False, None, None
        return read_policy(policy.data)

    def _controller_enabled(self) -> bool:
        return self._controller_config()[0]

    def _sync_progress(self) -> str | None:
        """An opaque marker that changes each time the controller completes a sync."""
        status = self._read(self.problem.kubectl.core_v1_api.read_namespaced_config_map, self.problem.status_configmap)
        if status is None:
            return None
        data = status.data or {}
        return f"{data.get('syncs')}/{data.get('last_sync')}"

    def _deferred_only(self) -> str | None:
        """Name the postponement if the controller is still armed on the wrong record.

        A later start is invisible to the observation loop, because the
        controller keeps completing syncs while it waits. A slower sync is
        caught here as well so that it is attributed, not merely timed out.
        """
        problem = self.problem
        injected_start = getattr(problem, "_effective_from", None)
        if injected_start is None:
            return None
        enforcing, start, interval = self._controller_config()
        if not enforcing:
            return None
        usage = self._read(problem.kubectl.core_v1_api.read_namespaced_config_map, problem.usage_configmap)
        recorded = read_usage(usage.data, problem.faulty_service) if usage is not None else None
        if recorded is None or recorded >= 1:
            return None

        if start > injected_start:
            return "effective_from moved later"
        if interval is not None and interval > problem.sync_interval_seconds:
            return "sync_interval_seconds raised"
        return None

    def _wait_for_login(self) -> dict | None:
        """Give logins time to come back; a probe that got no answer is retried like a failed login."""
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

    def _login(self):
        """Attempt one real login. Returns (True|False, output) or (None, failure verdict).

        A request that timed out, was refused or could not resolve the frontend
        is a failed login: the caller decides whether there is time to retry. A
        probe that could not run is a verdict in its own right, and a failure to
        exec into the pod at all is raised.
        """
        kubectl, namespace = self.problem.kubectl, self.problem.namespace
        pod = find_probe_pod(kubectl, namespace)
        if not pod:
            print("❌ No suitable probe pod found")
            return None, self.fail("no_probe_pod_available", namespace=namespace)
        statuses, output = wget_from_pod(kubectl, pod, namespace, _LOGIN_URL)
        if not statuses:
            if _PROBE_TOOLING_MISSING.search(output):
                return None, self.fail("http_probe_response_missing", pod=pod, output=output.strip()[:500])
            return False, output.strip()[-300:]
        return statuses[-1] == "200" and _LOGIN_OK in output, output.strip()[-300:]

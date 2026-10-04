import time

from sregym.conductor.oracles.base import Oracle
from sregym.service.rollout import deployment_rollout_complete


def _contains_command(command: list[str], wrong) -> bool:
    """Whether ``command`` still carries the injected ``wrong`` command.

    ``wrong`` is a marker string (matched against each token) or the injected
    argv list (matched as a contiguous run of tokens).
    """
    if isinstance(wrong, str):
        return any(wrong in token for token in command)
    wrong = list(wrong)
    if not wrong:
        return False
    return any(command[i : i + len(wrong)] == wrong for i in range(len(command) - len(wrong) + 1))


class WrongBinMitigationOracle(Oracle):
    importance = 1.0
    # Only used with ``problem.wrong_command``: how long to wait for the fixed rollout.
    rollout_timeout_s = 180.0

    def _rolled_out(self, kubectl, namespace: str) -> bool:
        deadline = time.monotonic() + self.rollout_timeout_s
        while True:
            deployment = kubectl.get_deployment(self.problem.faulty_service, namespace)
            if deployment_rollout_complete(deployment):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(5)

    def evaluate(self) -> dict:
        print("== Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        results = {}

        # Check if the deployment was updated to use the right binary
        # Command dictates which binary will be ran, we want to run /go/bin/profile and not /go/bin/geo.
        # Ports to other apps name their own binary with ``problem.expected_command``.
        expected_command = getattr(self.problem, "expected_command", "profile")
        # Optional: the injected command (marker string or argv list). When set, a
        # container whose command no longer carries it (e.g. the override was
        # removed so the image's default CMD runs) also counts as fixed, provided
        # the Deployment rolls out Ready.
        wrong_command = getattr(self.problem, "wrong_command", None)

        try:
            deployment = kubectl.get_deployment(self.problem.faulty_service, namespace)
            containers = deployment.spec.template.spec.containers
            needs_rollout = False

            for container in containers:
                command = list(container.command or [])
                if expected_command in command:
                    continue
                if wrong_command is not None and not _contains_command(command, wrong_command):
                    print(f"[i] Container '{container.name}' no longer runs the injected command: {command}")
                    needs_rollout = True
                    continue
                print(f"[❌] Deployment for container '{container.name}' is using wrong binary: {command}")
                # Compared against the command the injector replaced, so
                # this is the injected fault observed directly.
                return self.fail(
                    "fault_still_present",
                    container=container.name,
                    command=command,
                    expected=expected_command,
                )

            if needs_rollout and not self._rolled_out(kubectl, namespace):
                print(f"[❌] Deployment {self.problem.faulty_service} did not roll out Ready with its new command")
                return self.fail("fault_still_present", deployment=self.problem.faulty_service, rollout_complete=False)

            print("[✅] Deployment is using the correct binary.")
            results["success"] = True
            return results

        except Exception as e:
            print(f"[ERROR] Exception during evaluation: {e}")
            return self.fail_from_exception(e)

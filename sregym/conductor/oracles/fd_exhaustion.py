import subprocess
import time

from sregym.conductor.oracles.mitigation import MitigationOracle


class FDMitigationOracle(MitigationOracle):
    """
    Custom oracle for FileDescriptorExhaustion problem.
    First verifies that all pods are ready and rollouts are settled,
    then checks that the frontend logs show no 'too many open files' errors for a sustained period.
    """

    def __init__(self, problem, wait_seconds=15):
        super().__init__(problem=problem)
        self.namespace = problem.namespace
        self.faulty_service = problem.faulty_service
        self.kubectl = problem.kubectl
        self.wait_seconds = wait_seconds

    def _get_logs(self):
        cmd = ["kubectl", "logs", f"deployment/{self.faulty_service}", "-n", self.namespace, "--tail=50"]
        result = subprocess.run(cmd, capture_output=True, text=True)
        return result.stdout if result.returncode == 0 else ""

    def evaluate(self) -> dict:

        generic_result = super().evaluate()
        if not generic_result.get("success"):
            return generic_result

        print("✅ Generic mitigation checks passed.")

        print("Watching the logs. Expecting NO 'too many open files'...")

        if "too many open files" in self._get_logs().lower():
            print("❌ File descriptors exhausted. Mitigation failed!")
            return {"success": False}

        end_time = time.time() + self.wait_seconds
        absolute_timeout = time.time() + 30
        last_warning = 0

        while time.time() < end_time:
            if time.time() > absolute_timeout:
                print("❌ File descriptors exhausted. Mitigation failed!")
                return {"success": False}

            logs = self._get_logs()
            if "too many open files" in logs.lower():
                if time.time() - last_warning >= 5:
                    print("⚠️ 'too many open files' error still present.")
                    last_warning = time.time()
                end_time = time.time() + self.wait_seconds

            time.sleep(2)

        print("✅ No file descriptors exhaustion. Mitigation successful!")
        return {"success": True}

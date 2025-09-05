import shutil
import time

from srearena.conductor.oracles.detection import DetectionOracle
from srearena.conductor.problems.registry import ProblemRegistry
from srearena.service.apps.app_registry import AppRegistry
from srearena.service.khaos import KhaosController
from srearena.service.kubectl import KubeCtl
from srearena.service.telemetry.prometheus import Prometheus


class Conductor:
    def __init__(self):
        # core services
        self.problems = ProblemRegistry()
        self.kubectl = KubeCtl()
        self.prometheus = Prometheus()
        self.apps = AppRegistry()
        self.agent_name = None

        self.khaos = KhaosController(self.kubectl)

        self.problem = None
        self.detection_oracle = None
        self.problem_id = None
        self.problem = None
        self.app = None
        self.detection_oracle = None
        self.execution_start_time = None

        # grading flow state
        self.submission_stage = None  # "noop", "detection", "localization", "mitigation", "done"
        self.results = {}

    def register_agent(self, name="agent"):
        self.agent_name = name

    def dependency_check(self, binaries: list[str]):
        for b in binaries:
            if shutil.which(b) is None:
                raise RuntimeError(f"[❌] Required dependency '{b}' not found.")

    async def start_problem(self):
        """
        1) Provision infra & workload
        2) Flip to NO-OP grading stage
        """
        self.execution_start_time = time.time()
        self.problem = self.problems.get_problem_instance(self.problem_id)
        self.app = self.problem.app
        self.detection_oracle = DetectionOracle(self.problem)
        self.results = {}

        self.dependency_check(["kubectl", "helm"])
        print(f"[Session Start] Problem ID: {self.problem_id}")
        self.undeploy_app()  # Cleanup any leftovers
        self.deploy_app()

        self.submission_stage = "noop"
        print("✅ Deployment complete. Ready for submission.")

    async def submit(self, wrapped_cmd: str) -> dict:
        """
        Called by CLI or HTTP /submit.  Parses & grades the `submit(...)` call,
        advances submission_stage, records results—and when we hit “done”,
        triggers undeploy_app. Returns a snapshot of the results dict.
        """
        from srearena.conductor.parser import ResponseParser

        parser = ResponseParser()
        parsed = parser.parse(wrapped_cmd)
        if parsed["api_name"] != "submit":
            raise ValueError("Only `submit(...)` is supported.")
        sol = parsed["args"][0] if parsed["args"] else None

        # NO-OP
        if self.submission_stage == "noop":
            r = self.detection_oracle.evaluate(sol)
            self.results["NOOP Detection"] = r
            if r.get("reason") == "Invalid Format":
                return dict(self.results)

            self.problem.inject_fault()

            self.submission_stage = "detection"
            return dict(self.results)

        # DETECTION
        if self.submission_stage == "detection":
            r = self.detection_oracle.evaluate(sol)
            self.results["Detection"] = r
            self.results["TTD"] = time.time() - self.execution_start_time

            # if no further stages, finalize here
            if not self.problem.localization_oracle and not self.problem.mitigation_oracle:
                snapshot = dict(self.results)
                self.problem.recover_fault()
                self.undeploy_app()
                return snapshot

            # otherwise advance
            if self.problem.localization_oracle:
                self.submission_stage = "localization"
            else:
                self.submission_stage = "mitigation"
            return dict(self.results)

        # LOCALIZATION
        if self.submission_stage == "localization":
            r = self.problem.localization_oracle.evaluate(sol)
            self.results["Localization"] = r
            self.results["TTL"] = time.time() - self.execution_start_time

            if not self.problem.mitigation_oracle:
                snapshot = dict(self.results)
                self.problem.recover_fault()
                self.undeploy_app()
                return snapshot

            self.submission_stage = "mitigation"
            return dict(self.results)

        # MITIGATION
        if self.submission_stage == "mitigation":
            r = self.problem.mitigation_oracle.evaluate()
            self.results["Mitigation"] = r
            self.results["TTM"] = time.time() - self.execution_start_time

            snapshot = dict(self.results)
            self.problem.recover_fault()
            self.undeploy_app()
            return snapshot

        return dict(self.results)

    def deploy_app(self):
        """Kubectl + Prometheus + problem.app deployment."""
        self.submission_stage = "setup"
        print("Setting up metrics-server…")
        self.kubectl.exec_command(
            "kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/"
            "releases/latest/download/components.yaml"
        )
        self.kubectl.exec_command(
            "kubectl -n kube-system patch deployment metrics-server "
            "--type=json -p='["
            '{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"},'
            '{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-preferred-address-types=InternalIP"}'
            "]'"
        )
        self.kubectl.wait_for_ready("kube-system")

        print("Deploying Khaos DaemonSet...")
        self.khaos.ensure_deployed()

        print("Setting up OpenEBS…")
        self.kubectl.exec_command("kubectl apply -f https://openebs.github.io/charts/openebs-operator.yaml")
        self.kubectl.exec_command(
            "kubectl patch storageclass openebs-hostpath "
            '-p \'{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}\''
        )
        self.kubectl.wait_for_ready("openebs")

        print("Deploying Prometheus…")
        self.prometheus.deploy()

        print("Deploying and starting workload")
        self.problem.app.deploy()
        self.problem.app.start_workload()

    def undeploy_app(self):
        """Teardown problem.app and, if no other apps running, OpenEBS/Prometheus."""
        if self.problem:
            self.problem.app.cleanup()
        self.submission_stage = "done"

    def get_deployed_apps(self):
        deployed_apps = []
        for app_name in self.apps.get_app_names():
            namespace = self.apps.get_app_metadata(app_name)["Namespace"]
            if self.kubectl.get_namespace_deployment_status(namespace):
                deployed_apps.append(app_name)

        return deployed_apps

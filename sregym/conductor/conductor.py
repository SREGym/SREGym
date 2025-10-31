import logging
import shutil
import time
from pathlib import Path

import yaml

from dashboard.proxy import LogProxy
from sregym.conductor.oracles.detection import DetectionOracle
from sregym.conductor.problems.registry import ProblemRegistry
from sregym.conductor.utils import is_ordered_subset
from sregym.generators.fault.inject_remote_os import RemoteOSFaultInjector
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.generators.noise.transient_issues.transient_issues import FaultType, PodScope, TransientIssuesGenerator
from sregym.service.apps.app_registry import AppRegistry
from sregym.service.khaos import KhaosController
from sregym.service.kubectl import KubeCtl
from sregym.service.telemetry.prometheus import Prometheus


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

        self.tasklist = None
        self.logger = logging.getLogger("sregym-global")  # this is for dashboard
        self.local_logger = logging.getLogger("all.sregym.conductor")

        self.transient_config = {
            "switch": True,
            "min_duration": 40,
            "max_duration": 60,
            "fault_types": [FaultType.FAIL_SLOW, FaultType.FAIL_STOP],
            "scopes": [PodScope.TARGET_NAMESPACE],
            "interval_min": 20,
            "interval_max": 30,
        }

    def register_agent(self, name="agent"):
        self.agent_name = name

    def dependency_check(self, binaries: list[str]):
        for b in binaries:
            if shutil.which(b) is None:
                self.local_logger.error(f"Required dependency '{b}' not found.")
                raise RuntimeError(f"[❌] Required dependency '{b}' not found.")

    def get_tasklist(self):
        file_dir = Path(__file__).resolve().parent
        tasklist_path = file_dir / "tasklist.yml"

        # If tasklist file doesn't exist, default to running all tasks
        if not tasklist_path.exists():
            self.local_logger.info("No tasklist.yml found. Defaulting to running all tasks for this problem.")
            self.tasklist = ["noop", "detection", "localization", "mitigation", "done"]
            return

        with open(tasklist_path, "r") as f:
            tasklist = yaml.safe_load(f)
            if not tasklist:
                msg = "Badly formatted tasklist.yml"
                self.local_logger.error(msg)
                raise RuntimeError(msg)
            problems = tasklist["all"]["problems"]

        if self.problem_id not in (problems if problems else []):
            self.local_logger.warning(
                "problem_id not found in tasklist. Currently assuming that all tasks will be run."
            )
            self.tasklist = ["noop", "detection", "localization", "mitigation", "done"]
        else:
            problem_tasklist = problems[self.problem_id]
            if not problem_tasklist:
                msg = f"No tasks specified for {self.problem_id}"
                self.local_logger.error(msg)
                raise RuntimeError(msg)

            if not is_ordered_subset(problem_tasklist, ["detection", "localization", "mitigation"]):
                msg = f"Task list for {self.problem_id} is either out of order or has an unknown step"
                self.local_logger.error(msg)
                raise RuntimeError(msg)

            self.local_logger.info(
                f"Tasklist specified for {self.problem_id}. Configured tasks to run: {problem_tasklist}"
            )

            problem_tasklist.append("done")
            problem_tasklist.insert(0, "noop")
            self.tasklist = problem_tasklist

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
        self.local_logger.debug(f"Dependency check passed: kubectl, helm")

        self.local_logger.info(f"[Session Start] Problem ID: {self.problem_id}")
        self.logger.info(f"[STAGE] Start testing on problem: {self.problem_id}")

        if self.problem.requires_khaos() and self.kubectl.is_emulated_cluster():
            raise RuntimeError(
                f"Problem '{self.problem_id}' requires Khaos for eBPF-based fault injection, "
                "but Khaos cannot be deployed on emulated clusters (kind, minikube, k3d, etc.). "
                "Please use a real Kubernetes cluster to run this problem."
            )

        self.fix_kubernetes()

        self.get_tasklist()

        self.local_logger.info("Undeploying app leftovers...")
        self.undeploy_app()  # Cleanup any leftovers
        self.local_logger.info("App leftovers undeployed.")
        self.local_logger.info("Deploying app...")
        self.deploy_app()
        self.local_logger.info("App deployed.")

        self.submission_stage = self.tasklist[0]  # always noop

        self.local_logger.info(f"✅ Deployment complete. Ready for submission. Current stage is: {self.tasklist[0]}")

    async def submit(self, wrapped_cmd: str) -> dict:
        """
        Called by CLI or HTTP /submit.  Parses & grades the `submit(...)` call,
        advances submission_stage, records results—and when we hit “done”,
        triggers undeploy_app. Returns a snapshot of the results dict.
        """
        from sregym.conductor.parser import ResponseParser

        parser = ResponseParser()
        parsed = parser.parse(wrapped_cmd)
        if parsed["api_name"] != "submit":
            raise ValueError("Only `submit(...)` is supported.")
        sol = parsed["args"][0] if parsed["args"] else None

        # NO-OP
        if self.submission_stage == "noop":
            self.local_logger.info("Start Eval for Noop", extra={"sol": sol})
            r = self.detection_oracle.evaluate(sol)
            self.results["NOOP Detection"] = r
            self.logger.info(
                f"[EVAL] NOOP Detection {"Succeed" if self.results["NOOP Detection"]["success"] else "Failed"}\n"
            )
            if r.get("reason") == "Invalid Format":
                return dict(self.results)

            self.problem.inject_fault()

            self.logger.info(f"[ENV] Injected fault")

            # FIXME: Disabled until https://github.com/xlab-uiuc/SREGym/issues/296 is complete
            # self.configure_transient_issues()
            # if self.transient_config["switch"]:
            #     self._start_transient_issues()

        # DETECTION
        if self.submission_stage == "detection":
            self.local_logger.info("Start Eval for Detection", extra={"sol": sol})
            r = self.detection_oracle.evaluate(sol)
            self.results["Detection"] = r
            self.results["TTD"] = time.time() - self.execution_start_time
            self.logger.info(
                f"[EVAL] Detection {"Succeed" if self.results["Detection"]["success"] else "Failed"}\n TTD: {self.results['TTD']}"
            )

        # LOCALIZATION
        if self.submission_stage == "localization":
            self.local_logger.info("Start Eval for Localization", extra={"sol": sol})
            r = self.problem.localization_oracle.evaluate(sol)
            self.results["Localization"] = r
            self.results["TTL"] = time.time() - self.execution_start_time
            self.logger.info(
                f"[EVAL] Localization {"Succeed" if self.results["Localization"]["success"] else "Failed"}\n TTL: {self.results['TTL']}"
            )

        # MITIGATION
        if self.submission_stage == "mitigation":
            self.local_logger.info("Start Eval for Mitigation", extra={"sol": sol})
            r = self.problem.mitigation_oracle.evaluate()
            self.results["Mitigation"] = r
            self.results["TTM"] = time.time() - self.execution_start_time
            self.logger.info(
                f"[EVAL] Mitigation {"Succeed" if self.results["Mitigation"]["success"] else "Failed"}\n TTM: {self.results['TTM']}"
            )

        next_stage_idx = self.tasklist.index(self.submission_stage) + 1

        if self.tasklist[next_stage_idx] == "localization" and not self.problem.localization_oracle:
            self.local_logger.info("⏩ Localization oracle is not attached. Skipping localization.")
            next_stage_idx += 1

        if self.tasklist[next_stage_idx] == "mitigation" and not self.problem.mitigation_oracle:
            self.local_logger.info("⏩ Mitigation oracle is not attached. Skipping mitigation.")
            next_stage_idx += 1

        self.submission_stage = self.tasklist[next_stage_idx]

        if self.submission_stage != "done":
            self.local_logger.info(f"👉 Next task: {self.submission_stage}")
            self.logger.info(f"[STAGE] Go to stage {self.submission_stage}")
            return dict(self.results)
        else:
            snapshot = dict(self.results)

            self.logger.info(f"[STAGE] Done, recover fault")

            if self.transient_config["switch"]:
                self.transient_issue_generator.stop_continuous_injection()

            self.problem.recover_fault()
            self.logger.info(f"[STAGE] Undeploy app")
            self.undeploy_app()
            return snapshot

        return dict(self.results)

    def fix_kubernetes(self):
        self.local_logger.info("Fixing Kubernetes... to normal state.")
        self.local_logger.info("[FIX] Imbalance leftover if any")

        injector = VirtualizationFaultInjector(namespace="kube-system")
        injector.recover_daemon_set_image_replacement(
            daemon_set_name="kube-proxy", original_image="registry.k8s.io/kube-proxy:v1.31.13"
        )

        self.local_logger.info("[FIX] KubeletCrash leftover if any")
        injector = RemoteOSFaultInjector()
        injector.recover_kubelet_crash()
        self.local_logger.info("Fix Kubernetes completed.")

    def deploy_app(self):
        """Kubectl + Prometheus + problem.app deployment."""
        self.submission_stage = "setup"
        self.local_logger.info("[DEPLOY] Setting up metrics-server…")
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

        # Only deploy Khaos if the problem requires it
        if self.problem and self.problem.requires_khaos():
            self.local_logger.info("[DEPLOY] Deploying Khaos DaemonSet...")
            self.khaos.ensure_deployed()

        self.local_logger.info("[DEPLOY] Setting up OpenEBS…")
        self.kubectl.exec_command("kubectl apply -f https://openebs.github.io/charts/openebs-operator.yaml")
        self.kubectl.exec_command(
            "kubectl patch storageclass openebs-hostpath "
            '-p \'{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}\''
        )
        self.kubectl.wait_for_ready("openebs")

        self.local_logger.info("[DEPLOY] Deploying Prometheus…")
        self.prometheus.deploy()

        self.logger.info(f"[ENV] Set up neccesary components: metrics-server, Khaos, OpenEBS, Prometheus")

        self.local_logger.info("[DEPLOY] Deploying and starting workload")
        self.problem.app.deploy()
        self.logger.info(f"[ENV] Deploy application: {self.problem.app.name}")

        self.problem.app.start_workload()
        self.logger.info(f"[ENV] Start workload")

    def undeploy_app(self):
        """Teardown problem.app and, if no other apps running, OpenEBS/Prometheus."""
        if self.problem:
            self.problem.app.cleanup()

    def get_deployed_apps(self):
        deployed_apps = []
        for app_name in self.apps.get_app_names():
            namespace = self.apps.get_app_metadata(app_name)["Namespace"]
            if self.kubectl.get_namespace_deployment_status(namespace):
                deployed_apps.append(app_name)

        return deployed_apps

    def configure_transient_issues(self):
        """
        Read transient issues configuration from sregym/generators/noise/transient_issues/configuration.yml file.
        """
        import os

        import yaml

        from sregym.generators.noise.transient_issues.transient_issues import FaultType, PodScope

        config_path = os.path.join(os.path.dirname(__file__), "../generators/noise/transient_issues/configuration.yml")
        config_path = os.path.abspath(config_path)

        try:
            with open(config_path, "r") as f:
                config = yaml.safe_load(f)
        except Exception as e:
            self.local_logger.error(f"[❌] Failed to load configuration: {e}")
            return

        # Parse configuration and convert to required types
        def parse_fault_types(types):
            if not types:
                return []
            return [getattr(FaultType, t) if isinstance(t, str) else t for t in types]

        def parse_scopes(scopes):
            if not scopes:
                return []
            return [getattr(PodScope, s) if isinstance(s, str) else s for s in scopes]

        self.transient_config["switch"] = config.get("switch", True)
        self.transient_config["min_duration"] = config.get("min_duration", 40)
        self.transient_config["max_duration"] = config.get("max_duration", 60)
        self.transient_config["fault_types"] = parse_fault_types(config.get("fault_types", ["FAIL_SLOW", "FAIL_STOP"]))
        self.transient_config["scopes"] = parse_scopes(config.get("scopes", ["TARGET_NAMESPACE"]))
        self.transient_config["interval_min"] = config.get("interval_min", 20)
        self.transient_config["interval_max"] = config.get("interval_max", 30)

        print(f"✅ Transient issues configuration loaded from {config_path}: {self.transient_config}")

    def _start_transient_issues(self):
        """Start transient issues with current configuration"""
        if self.problem:
            faulty_service = (
                self.problem.faulty_service
                if isinstance(self.problem.faulty_service, (list, tuple))
                else [self.problem.faulty_service]
            )
            self.transient_issue_generator = TransientIssuesGenerator(
                namespace=self.problem.app.namespace,
                target_services=faulty_service,
                min_duration=self.transient_config["min_duration"],
                max_duration=self.transient_config["max_duration"],
            )
            self.transient_issue_generator.start_continuous_injection(
                fault_types=self.transient_config["fault_types"],
                scopes=self.transient_config["scopes"],
                interval_min=self.transient_config["interval_min"],
                interval_max=self.transient_config["interval_max"],
            )

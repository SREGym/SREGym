import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

from sregym.conductor.constants import StartProblemResult
from sregym.conductor.oracles.detection import DetectionOracle
from sregym.conductor.oracles.diagnosis_oracle import DiagnosisOracle
from sregym.conductor.problems.registry import ProblemRegistry
from sregym.conductor.utils import is_ordered_subset
from sregym.generators.fault.inject_remote_os import RemoteOSFaultInjector
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.generators.noise.manager import get_noise_manager
from sregym.service.apps.app_registry import AppRegistry
from sregym.service.cluster_state import ClusterStateManager
from sregym.service.dm_dust_manager import DmDustManager
from sregym.service.dm_flakey_manager import DmFlakeyManager
from sregym.service.k8s_proxy import KubernetesAPIProxy
from sregym.service.khaos import KhaosController
from sregym.service.kubectl import KubeCtl
from sregym.service.telemetry.loki import Loki
from sregym.service.telemetry.prometheus import Prometheus


@dataclass
class ConductorConfig:
    """Configuration for Conductor deployment options."""

    deploy_loki: bool = True


class Conductor:
    def __init__(self, config: ConductorConfig | None = None):
        self.config = config or ConductorConfig()

        # core services
        self.problems = ProblemRegistry()
        self.kubectl = KubeCtl()
        self.prometheus = Prometheus()
        self.loki = Loki()
        self.apps = AppRegistry()
        self.agent_name = None

        self.khaos = KhaosController(self.kubectl)
        self.dm_dust_manager = DmDustManager(self.kubectl)
        self.dm_flakey_manager = DmFlakeyManager(self.kubectl)
        self.cluster_state = ClusterStateManager(self.kubectl)
        self._baseline_captured = False

        # Kubernetes API proxy to hide chaos engineering namespaces from agents
        self.k8s_proxy = KubernetesAPIProxy(
            hidden_namespaces={"chaos-mesh", "khaos"},
            listen_port=16443,
        )
        self._agent_kubeconfig_path: str | None = None

        self.problem_id: str | None = None
        self.problem = None
        self.app = None
        self.detection_oracle = None
        self.execution_start_time: float = 0.0

        # grading flow state
        # submission_stage reflects the current stage (e.g., "diagnosis", "mitigation") or "done"
        self.submission_stage = None
        self.results = {}

        self.tasklist = None
        self.logger = logging.getLogger("all.sregym.conductor")

        self.stage_sequence: list[dict] = []
        self.current_stage_index: int = 0
        self.waiting_for_agent: bool = False
        self.fault_injected: bool = False

    @property
    def current_problem(self):
        """Return the current problem, raising if none is loaded."""
        if self.problem is None:
            raise RuntimeError("No problem is loaded")
        return self.problem

    def register_agent(self, name="agent"):
        self.agent_name = name

    def start_k8s_proxy(self):
        """
        Start the Kubernetes API proxy that hides chaos engineering namespaces.
        Should be called before launching agents.
        """
        self.logger.info("Starting Kubernetes API filtering proxy...")
        self.k8s_proxy.start()
        self._agent_kubeconfig_path = self.k8s_proxy.generate_agent_kubeconfig()
        self.logger.info(f"Agent kubeconfig generated at: {self._agent_kubeconfig_path}")

    def stop_k8s_proxy(self):
        """Stop the Kubernetes API proxy."""
        self.logger.info("Stopping Kubernetes API filtering proxy...")
        self.k8s_proxy.stop()
        self._agent_kubeconfig_path = None

    def get_agent_kubeconfig_path(self) -> str | None:
        """
        Get the path to the kubeconfig file that agents should use.
        This kubeconfig points to the filtering proxy that hides chaos namespaces.
        """
        return self._agent_kubeconfig_path

    def dependency_check(self, binaries: list[str]):
        for b in binaries:
            if shutil.which(b) is None:
                self.logger.error(f"Required dependency '{b}' not found.")
                raise RuntimeError(f"[❌] Required dependency '{b}' not found.")

    def get_problem_stages(self):
        file_dir = Path(__file__).resolve().parent
        tasklist_path = file_dir / "tasklist.yml"

        # If tasklist file doesn't exist, default to running diagnosis + mitigation
        if not tasklist_path.exists():
            self.logger.info("No tasklist.yml found. Defaulting to running diagnosis and mitigation for this problem.")
            self.tasklist = ["diagnosis", "mitigation"]
            return

        with open(tasklist_path) as f:
            tasklist = yaml.safe_load(f)
            if not tasklist:
                msg = "Badly formatted tasklist.yml"
                self.logger.error(msg)
                raise RuntimeError(msg)
            problems = tasklist["all"]["problems"]

        if self.problem_id not in (problems if problems else []):
            self.logger.warning("problem_id not found in tasklist. Defaulting to running diagnosis and mitigation.")
            self.tasklist = ["diagnosis", "mitigation"]
        else:
            problem_tasklist = problems[self.problem_id]
            if not problem_tasklist:
                msg = f"No tasks specified for {self.problem_id}"
                self.logger.error(msg)
                raise RuntimeError(msg)

            if not is_ordered_subset(problem_tasklist, ["diagnosis", "mitigation"]):
                msg = f"Task list for {self.problem_id} is either out of order or has an unknown step (allowed: diagnosis, mitigation)"
                self.logger.error(msg)
                raise RuntimeError(msg)

            self.logger.info(f"Tasklist specified for {self.problem_id}. Configured stages to run: {problem_tasklist}")

            # Use the tasklist as-is (stage names: diagnosis, mitigation)
            self.tasklist = problem_tasklist

    def _build_stage_sequence(self):
        """Build the sequence of stages (diagnosis, mitigation) based on tasklist and available oracles."""
        self.stage_sequence = []
        self.current_stage_index = 0
        self.waiting_for_agent = False
        self.fault_injected = False

        if not self.tasklist:
            self.logger.warning("Empty tasklist; no stages configured for this problem.")
            return

        # Map stage names to their evaluation functions
        stage_definitions = {
            "diagnosis": self._evaluate_diagnosis,
            "mitigation": self._evaluate_mitigation,
        }

        # Determine which stages are actually available (oracle attached)
        for name in self.tasklist:
            if name not in stage_definitions:
                self.logger.warning(f"Unknown stage '{name}' in tasklist; skipping.")
                continue

            if name == "diagnosis":
                if getattr(self.problem, "diagnosis_oracle", None):
                    self.stage_sequence.append(
                        {
                            "name": name,
                            "evaluation": stage_definitions[name],
                        }
                    )
                else:
                    self.logger.info("⏩ Diagnosis oracle is not attached. Skipping diagnosis.")

            elif name == "mitigation":
                if getattr(self.problem, "mitigation_oracle", None):
                    self.stage_sequence.append(
                        {
                            "name": name,
                            "evaluation": stage_definitions[name],
                        }
                    )
                else:
                    self.logger.info("⏩ Mitigation oracle is not attached. Skipping mitigation.")

        if not self.stage_sequence:
            self.logger.warning(
                "No stages left after checking oracles. This problem will complete without agent interaction."
            )

    def _inject_fault(self):
        """Inject fault and prepare diagnosis checkpoint if available."""
        problem = self.current_problem
        problem.inject_fault()
        self.logger.info("[ENV] Injected fault")
        self.fault_injected = True

        # Prepare diagnosis checkpoint if available, after fault injection but before agent stages
        if (
            hasattr(problem, "diagnosis_oracle")
            and problem.diagnosis_oracle
            and isinstance(problem.diagnosis_oracle, DiagnosisOracle)
        ):
            problem.diagnosis_oracle.load_diagnosis_checkpoint()
            self.logger.info("Diagnosis checkpoint loaded after fault injection.")

    def _evaluate_diagnosis(self, solution):
        """Evaluation logic for diagnosis stage."""
        problem = self.current_problem

        self.logger.info("Start Eval for Diagnosis", extra={"sol": solution})
        r = problem.diagnosis_oracle.evaluate(solution)
        self.results["Diagnosis"] = r
        self.results["TTL"] = time.time() - self.execution_start_time
        self.logger.info(
            f"[EVAL] Diagnosis "
            f"{'Succeed' if self.results['Diagnosis']['success'] else 'Failed'}\n "
            f"TTL: {self.results['TTL']}"
        )
        return r

    def _evaluate_mitigation(self, solution):
        """Evaluation logic for mitigation stage."""
        problem = self.current_problem
        # Currently mitigation_oracle.evaluate() does not take the agent solution directly.
        self.logger.info("Start Eval for Mitigation", extra={"sol": solution})
        r = problem.mitigation_oracle.evaluate()
        self.results["Mitigation"] = r
        self.results["TTM"] = time.time() - self.execution_start_time
        self.logger.info(
            f"[EVAL] Mitigation "
            f"{'Succeed' if self.results['Mitigation']['success'] else 'Failed'}\n "
            f"TTM: {self.results['TTM']}"
        )
        return r

    def _advance_to_next_stage(self, start_index: int = 0):
        """
        Advance to the next stage starting from start_index.
        If there are more stages, set up for agent submission.
        Otherwise, finish the problem.
        """
        self.waiting_for_agent = False
        self.current_stage_index = start_index

        if not self.stage_sequence:
            self.logger.info("No stages configured; finishing problem immediately.")
            self._finish_problem()
            return

        # Inject fault before the first stage if not already done
        if start_index == 0 and not self.fault_injected:
            self._inject_fault()

        if start_index < len(self.stage_sequence):
            stage = self.stage_sequence[start_index]
            stage_name: str = stage["name"]

            self.logger.debug(f"Advancing to stage '{stage_name}' and waiting for agent.")
            self.waiting_for_agent = True
            self.submission_stage = stage_name
            self.logger.info(f"[STAGE] Go to stage {self.submission_stage}")

            # Update NoiseManager stage
            try:
                nm = get_noise_manager()
                nm.set_stage(stage_name)
            except Exception as e:
                self.logger.warning(f"Failed to set NoiseManager stage: {e}")
        else:
            # No more stages; finish the problem
            self._finish_problem()

    def _finish_problem(self):
        self.logger.info("[STAGE] Done, recover fault")

        # Stop noises
        try:
            nm = get_noise_manager()
            nm.stop()
        except Exception as e:
            self.logger.warning(f"Failed to stop NoiseManager: {e}")

        if self.problem:
            self.problem.recover_fault()

        self.logger.info("[STAGE] Undeploy app")
        self.undeploy_app()

        # Reconcile cluster state to baseline to clean up any changes made by the agent
        if self._baseline_captured:
            self.logger.info("[STAGE] Reconciling cluster state to baseline")
            try:
                changes = self.cluster_state.reconcile_to_baseline()
                if any(v for v in changes.values() if v):
                    self.logger.info(f"Cluster state reconciliation changes: {changes}")
            except Exception as e:
                self.logger.warning(f"Failed to reconcile cluster state: {e}")

        # Set to "done" after all cleanup is complete to prevent race condition
        # where the next problem starts before cleanup finishes
        self.submission_stage = "done"

    async def start_problem(self) -> StartProblemResult:
        """
        1) Provision infra & workload
        2) Initialize Act registry and execute initial GymActs and first AgentAct precondition

        Returns:
            StartProblemResult: Result status indicating success or skip reason
        """
        if self.problem_id is None:
            raise RuntimeError("Cannot start problem: problem_id is not set")
        self.execution_start_time = time.time()
        self.problem = self.problems.get_problem_instance(self.problem_id)
        self.app = self.problem.app
        self.detection_oracle = DetectionOracle(self.problem)
        self.results = {}

        self.dependency_check(["kubectl", "helm"])
        self.logger.debug("Dependency check passed: kubectl, helm")

        self.logger.info(f"[Session Start] Problem ID: {self.problem_id}")
        self.logger.info(f"[STAGE] Start testing on problem: {self.problem_id}")

        if self.problem.requires_khaos() and self.kubectl.is_emulated_cluster():
            self.logger.warning(
                f"Problem '{self.problem_id}' requires Khaos for eBPF-based fault injection, "
                "but Khaos cannot be deployed on emulated clusters (kind, minikube, k3d, etc.). "
                "Skipping this problem."
            )
            return StartProblemResult.SKIPPED_KHAOS_REQUIRED

        self.fix_kubernetes()

        self.get_problem_stages()
        self._build_stage_sequence()

        self.logger.info("Undeploying app leftovers...")
        self.undeploy_app()  # Cleanup any leftovers
        self.logger.info("App leftovers undeployed.")
        self.logger.info("Deploying app...")
        self.deploy_app()
        self.logger.info("App deployed.")

        # Update NoiseManager with problem context
        try:
            nm = get_noise_manager()
            context = {
                "namespace": self.app.namespace,
                "app_name": self.app.name,
                # We can add more info here if needed, e.g. service list
            }
            nm.set_problem_context(context)
            nm.start_background_noises()
        except Exception as e:
            self.logger.warning(f"Failed to update NoiseManager context: {e}")

        # After deployment, advance to the first stage
        self._advance_to_next_stage(start_index=0)

        if self.submission_stage and self.submission_stage != "done":
            self.logger.info(f"✅ Deployment complete. Ready for submission. Current stage is: {self.submission_stage}")
        else:
            self.logger.info(
                "✅ Deployment complete. No stages configured; problem will complete without agent submission."
            )
        return StartProblemResult.SUCCESS

    async def submit(self, wrapped_cmd: str) -> dict:
        """
        Called by CLI or HTTP /submit.  Parses & grades the `submit(...)` call,
        advances submission_stage, records results—and when we hit "done",
        triggers undeploy_app. Returns a snapshot of the results dict.
        """
        from sregym.conductor.parser import ResponseParser

        parser = ResponseParser()
        parsed = parser.parse(wrapped_cmd)
        if parsed["api_name"] != "submit":
            raise ValueError("Only `submit(...)` is supported.")
        sol = parsed["args"][0] if parsed["args"] else None

        # If all tasks are already completed, simply return the final snapshot.
        if self.submission_stage == "done":
            self.logger.info("All tasks already completed; ignoring new submission.")
            return dict(self.results)

        if not self.stage_sequence:
            self.logger.warning("submit() called but no stages are configured; returning current results.")
            return dict(self.results)

        if not self.waiting_for_agent:
            self.logger.error(
                "submit() called when conductor is not waiting for a submission. "
                f"Current submission_stage={self.submission_stage}"
            )
            raise RuntimeError("Conductor is not currently waiting for an agent submission.")

        current_stage = self.stage_sequence[self.current_stage_index]
        stage_name: str = current_stage["name"]
        self.logger.info(f"Evaluating stage '{stage_name}'", extra={"sol": sol})

        # Stop noise before evaluation to ensure clean environment
        try:
            nm = get_noise_manager()
            self.logger.info("Stopping noise manager before evaluation...")
            nm.stop()
        except Exception as e:
            self.logger.warning(f"Failed to stop noise manager: {e}")

        # Run the evaluation function for the current stage
        current_stage["evaluation"](sol)

        # After evaluation, advance to the next stage (if any)
        next_index = self.current_stage_index + 1
        self._advance_to_next_stage(start_index=next_index)

        # Restart noise if there are more stages
        if self.submission_stage != "done":
            try:
                nm = get_noise_manager()
                self.logger.info("Restarting noise manager for next stage...")
                nm.start_background_noises()
            except Exception as e:
                self.logger.warning(f"Failed to restart noise manager: {e}")

        return dict(self.results)

    def fix_kubernetes(self):
        self.logger.info("Fixing Kubernetes... to normal state.")
        self.logger.info("[FIX] Imbalance leftover if any")

        injector = VirtualizationFaultInjector(namespace="kube-system")
        injector.recover_daemon_set_image_replacement(
            daemon_set_name="kube-proxy", original_image="registry.k8s.io/kube-proxy:v1.31.13"
        )

        self.logger.info("[FIX] KubeletCrash leftover if any")
        injector = RemoteOSFaultInjector()
        injector.recover_kubelet_crash()

        self.logger.info("[FIX] Stale CoreDNS NXDOMAIN templates if any")
        injector = VirtualizationFaultInjector(namespace="kube-system")
        try:
            injector.recover_all_nxdomain_templates()
        except Exception as e:
            self.logger.error(f"Failed to recover CoreDNS NXDOMAIN templates: {e}")
        self.logger.info("Fix Kubernetes completed.")

    def deploy_app(self):
        """Kubectl + Prometheus + problem.app deployment."""
        problem = self.current_problem
        self.submission_stage = "setup"
        self.logger.info("[DEPLOY] Setting up metrics-server…")
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
        if problem.requires_khaos():
            self.logger.info("[DEPLOY] Deploying Khaos DaemonSet...")
            self.khaos.ensure_deployed()

        self.logger.info("[DEPLOY] Setting up OpenEBS…")
        self.kubectl.exec_command("kubectl apply -f https://openebs.github.io/charts/openebs-operator.yaml")
        self.kubectl.exec_command(
            "kubectl patch storageclass openebs-hostpath "
            '-p \'{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}\''
        )
        self.kubectl.wait_for_ready("openebs")

        print("Setting up OpenEBS LocalPV-Device…")
        device_sc_yaml = """
        apiVersion: storage.k8s.io/v1
        kind: StorageClass
        metadata:
        name: openebs-device
        annotations:
            openebs.io/cas-type: local
        provisioner: openebs.io/local
        parameters:
        localpvType: "device"
        volumeBindingMode: WaitForFirstConsumer
        """
        self.kubectl.exec_command("kubectl apply -f - <<EOF\n" + device_sc_yaml + "\nEOF")

        self.logger.info("[DEPLOY] Deploying Prometheus…")
        self.prometheus.deploy()

        if self.config.deploy_loki:
            self.logger.info("[DEPLOY] Deploying Loki…")
            self.loki.deploy()
        else:
            self.logger.info("[DEPLOY] Skipping Loki deployment (external harness mode)")

        # Set up fault injection infrastructure based on problem type
        # Only one can be active at /var/openebs/local at a time
        problem_name = problem.__class__.__name__

        if "LatentSectorError" in problem_name:
            print("Setting up dm-dust infrastructure for LSE fault injection...")
            self.dm_dust_manager.setup_openebs_dm_dust_infrastructure()
        elif "SilentDataCorruption" in problem_name:
            print("Setting up dm-flakey infrastructure for Silent Data Corruption fault injection...")
            self.dm_flakey_manager.setup_openebs_dm_flakey_infrastructure()

        self.logger.info("[ENV] Set up necessary components: metrics-server, Khaos, OpenEBS, Prometheus, Loki")

        # Capture cluster baseline state after infrastructure is deployed but before app deployment
        # This allows us to reset the cluster to a clean state after each problem
        if not self._baseline_captured:
            self.logger.info("[DEPLOY] Capturing cluster baseline state...")
            self.cluster_state.capture_baseline()
            self._baseline_captured = True

        self.logger.info("[DEPLOY] Deploying and starting workload")
        problem.app.deploy()
        self.logger.info(f"[ENV] Deploy application: {problem.app.name}")

        problem.app.start_workload()
        self.logger.info("[ENV] Start workload")

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

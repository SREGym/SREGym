import logging
import shutil
import time
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
from sregym.service.dm_dust_manager import DmDustManager
from sregym.service.dm_flakey_manager import DmFlakeyManager
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
        self.dm_dust_manager = DmDustManager(self.kubectl)
        self.dm_flakey_manager = DmFlakeyManager(self.kubectl)

        self.problem = None
        self.detection_oracle = None
        self.problem_id = None
        self.problem = None
        self.app = None
        self.detection_oracle = None
        self.execution_start_time = None

        # grading flow state
        # submission_stage reflects the current AgentAct name (e.g., "diagnosis", "mitigation") or "done"
        self.submission_stage = None
        self.results = {}

        self.tasklist = None
        self.logger = logging.getLogger("sregym-global")  # this is for dashboard
        self.local_logger = logging.getLogger("all.sregym.conductor")

        self.act_sequence: list[dict] = []
        self.current_act_index: int = 0
        self.current_agent_act_index: int | None = None
        self.waiting_for_agent: bool = False

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

        # If tasklist file doesn't exist, default to running diagnosis + mitigation
        if not tasklist_path.exists():
            self.local_logger.info(
                "No tasklist.yml found. Defaulting to running diagnosis and mitigation for this problem."
            )
            self.tasklist = ["diagnosis", "mitigation"]
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
                "problem_id not found in tasklist. Defaulting to running diagnosis and mitigation."
            )
            self.tasklist = ["diagnosis", "mitigation"]
        else:
            problem_tasklist = problems[self.problem_id]
            if not problem_tasklist:
                msg = f"No tasks specified for {self.problem_id}"
                self.local_logger.error(msg)
                raise RuntimeError(msg)

            if not is_ordered_subset(problem_tasklist, ["diagnosis", "mitigation"]):
                msg = f"Task list for {self.problem_id} is either out of order or has an unknown step (allowed: diagnosis, mitigation)"
                self.local_logger.error(msg)
                raise RuntimeError(msg)

            self.local_logger.info(
                f"Tasklist specified for {self.problem_id}. Configured AgentActs to run: {problem_tasklist}"
            )

            # Use the tasklist as-is (only AgentAct names, e.g., diagnosis, mitigation)
            self.tasklist = problem_tasklist

    def _build_act_sequence(self):
        self.act_sequence = []
        self.current_act_index = 0
        self.current_agent_act_index = None
        self.waiting_for_agent = False

        if not self.tasklist:
            self.local_logger.warning("Empty tasklist; no AgentActs configured for this problem.")
            return

        # Map AgentAct names to their precondition/evaluation functions
        agent_act_definitions = {
            "diagnosis": {
                "precondition": self._precondition_diagnosis,
                "evaluation": self._evaluate_diagnosis,
            },
            "mitigation": {
                "precondition": self._precondition_mitigation,
                "evaluation": self._evaluate_mitigation,
            },
        }

        # Determine which AgentActs are actually available (oracle attached)
        configured_agent_acts: list[dict] = []
        for name in self.tasklist:
            if name not in agent_act_definitions:
                self.local_logger.warning(f"Unknown AgentAct '{name}' in tasklist; skipping.")
                continue

            if name == "diagnosis":
                if getattr(self.problem, "diagnosis_oracle", None):
                    configured_agent_acts.append(
                        {
                            "type": "AgentAct",
                            "name": name,
                            "precondition": agent_act_definitions[name]["precondition"],
                            "evaluation": agent_act_definitions[name]["evaluation"],
                        }
                    )
                else:
                    self.local_logger.info("⏩ Diagnosis oracle is not attached. Skipping diagnosis.")

            elif name == "mitigation":
                if getattr(self.problem, "mitigation_oracle", None):
                    configured_agent_acts.append(
                        {
                            "type": "AgentAct",
                            "name": name,
                            "precondition": agent_act_definitions[name]["precondition"],
                            "evaluation": agent_act_definitions[name]["evaluation"],
                        }
                    )
                else:
                    self.local_logger.info("⏩ Mitigation oracle is not attached. Skipping mitigation.")

        if not configured_agent_acts:
            self.local_logger.warning(
                "No AgentActs left after checking oracles. This problem will complete without agent interaction."
            )
            return

        # Default GymAct: inject fault before the first AgentAct precondition
        self.act_sequence.append(
            {
                "type": "GymAct",
                "name": "inject_fault",
                "op": self._gymact_inject_fault,
            }
        )

        # Append AgentActs in order
        self.act_sequence.extend(configured_agent_acts)

    def _gymact_inject_fault(self):
        self.problem.inject_fault()
        self.logger.info("[ENV] Injected fault")

        # Prepare diagnosis checkpoint if available, after fault injection but before agent acts
        if (
            hasattr(self.problem, "diagnosis_oracle")
            and self.problem.diagnosis_oracle
            and isinstance(self.problem.diagnosis_oracle, DiagnosisOracle)
        ):
            self.problem.diagnosis_oracle.load_localization_checkpoint()
            self.local_logger.info("Diagnosis checkpoint loaded after fault injection.")

    # -------- AgentAct: diagnosis --------
    def _precondition_diagnosis(self):
        self.local_logger.info("Precondition for Diagnosis AgentAct executed. No real action.")

    def _evaluate_diagnosis(self, solution):
        """Evaluation logic for diagnosis AgentAct."""
        self.local_logger.info("Start Eval for Diagnosis", extra={"sol": solution})
        r = self.problem.diagnosis_oracle.evaluate(solution)
        self.results["Diagnosis"] = r
        self.results["TTL"] = time.time() - self.execution_start_time
        self.logger.info(
            f"[EVAL] Diagnosis "
            f"{'Succeed' if self.results['Diagnosis']['success'] else 'Failed'}\n "
            f"TTL: {self.results['TTL']}"
        )
        return r

    # -------- AgentAct: mitigation --------
    def _precondition_mitigation(self):
        self.local_logger.info("Precondition for Mitigation AgentAct executed. No real action.")

    def _evaluate_mitigation(self, solution):
        """Evaluation logic for mitigation AgentAct."""
        # Currently mitigation_oracle.evaluate() does not take the agent solution directly.
        self.local_logger.info("Start Eval for Mitigation", extra={"sol": solution})
        r = self.problem.mitigation_oracle.evaluate()
        self.results["Mitigation"] = r
        self.results["TTM"] = time.time() - self.execution_start_time
        self.logger.info(
            f"[EVAL] Mitigation "
            f"{'Succeed' if self.results['Mitigation']['success'] else 'Failed'}\n "
            f"TTM: {self.results['TTM']}"
        )
        return r

    def _advance_to_next_agent_act_precondition(self, start_index: int = 0):
        """
        Execute Acts sequentially starting from start_index until:
          - The precondition of the next AgentAct is executed (inclusive), then wait for agent submission; or
          - There are no more AgentActs, in which case finish the problem.
        """
        self.current_agent_act_index = None
        self.waiting_for_agent = False
        self.current_act_index = start_index

        if not self.act_sequence:
            self.local_logger.info("No Acts configured; finishing problem immediately.")
            self._finish_problem()
            return

        i = start_index
        while i < len(self.act_sequence):
            act = self.act_sequence[i]
            act_type = act.get("type")
            act_name = act.get("name")

            if act_type == "GymAct":
                self.local_logger.debug(f"Executing GymAct '{act_name}'")
                act["op"]()
                i += 1
                continue

            if act_type == "AgentAct":
                self.local_logger.debug(f"Executing precondition for AgentAct '{act_name}' and waiting for agent.")
                act["precondition"]()
                self.current_agent_act_index = i
                self.waiting_for_agent = True
                self.submission_stage = act_name
                self.current_act_index = i
                self.logger.info(f"[STAGE] Go to stage {self.submission_stage}")

                # Update NoiseManager stage
                try:
                    nm = get_noise_manager()
                    nm.set_stage(self.submission_stage)
                except Exception as e:
                    self.local_logger.warning(f"Failed to set NoiseManager stage: {e}")

                return

            self.local_logger.warning(f"Unknown Act type '{act_type}' for Act '{act_name}'; skipping.")
            i += 1

        # No more AgentActs; finish the problem
        self._finish_problem()

    def _finish_problem(self):
        self.submission_stage = "done"

        self.logger.info(f"[STAGE] Done, recover fault")

        # Stop noises
        try:
            nm = get_noise_manager()
            nm.stop()
        except Exception as e:
            self.local_logger.warning(f"Failed to stop NoiseManager: {e}")

        if self.problem:
            self.problem.recover_fault()

        self.logger.info(f"[STAGE] Undeploy app")
        self.undeploy_app()

    async def start_problem(self) -> StartProblemResult:
        """
        1) Provision infra & workload
        2) Initialize Act registry and execute initial GymActs and first AgentAct precondition

        Returns:
            StartProblemResult: Result status indicating success or skip reason
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
            self.local_logger.warning(
                f"Problem '{self.problem_id}' requires Khaos for eBPF-based fault injection, "
                "but Khaos cannot be deployed on emulated clusters (kind, minikube, k3d, etc.). "
                "Skipping this problem."
            )
            return StartProblemResult.SKIPPED_KHAOS_REQUIRED

        self.fix_kubernetes()

        self.get_tasklist()
        self._build_act_sequence()

        self.local_logger.info("Undeploying app leftovers...")
        self.undeploy_app()  # Cleanup any leftovers
        self.local_logger.info("App leftovers undeployed.")
        self.local_logger.info("Deploying app...")
        self.deploy_app()
        self.local_logger.info("App deployed.")

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
            self.local_logger.warning(f"Failed to update NoiseManager context: {e}")

        # After deployment, execute Acts until the first AgentAct precondition is reached.
        self._advance_to_next_agent_act_precondition(start_index=0)

        if self.submission_stage and self.submission_stage != "done":
            self.local_logger.info(
                f"✅ Deployment complete. Ready for submission. Current stage is: {self.submission_stage}"
            )
        else:
            self.local_logger.info(
                "✅ Deployment complete. No AgentAct configured; problem will complete without agent submission."
            )
        return StartProblemResult.SUCCESS

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

        # If all tasks are already completed, simply return the final snapshot.
        if self.submission_stage == "done":
            self.local_logger.info("All tasks already completed; ignoring new submission.")
            return dict(self.results)

        if not self.act_sequence:
            self.local_logger.warning("submit() called but no Acts are configured; returning current results.")
            return dict(self.results)

        if self.current_agent_act_index is None or not self.waiting_for_agent:
            self.local_logger.error(
                "submit() called when conductor is not waiting for an AgentAct evaluation. "
                f"Current submission_stage={self.submission_stage}"
            )
            raise RuntimeError("Conductor is not currently waiting for an agent submission.")

        current_act = self.act_sequence[self.current_agent_act_index]
        if current_act.get("type") != "AgentAct":
            self.local_logger.error(
                f"Internal error: current_act at index {self.current_agent_act_index} is not an AgentAct."
            )
            raise RuntimeError("Invalid Act configuration.")

        act_name = current_act.get("name")
        self.local_logger.info(f"Evaluating AgentAct '{act_name}'", extra={"sol": sol})

        # Stop noise before evaluation to ensure clean environment
        try:
            nm = get_noise_manager()
            self.local_logger.info("Stopping noise manager before evaluation...")
            nm.stop()
        except Exception as e:
            self.local_logger.warning(f"Failed to stop noise manager: {e}")

        # Run the evaluation function for the current AgentAct
        current_act["evaluation"](sol)

        # After evaluation, advance to the next AgentAct precondition (if any)
        next_index = self.current_agent_act_index + 1
        self._advance_to_next_agent_act_precondition(start_index=next_index)

        # Restart noise if there are more stages
        if self.submission_stage != "done":
            try:
                nm = get_noise_manager()
                self.local_logger.info("Restarting noise manager for next stage...")
                nm.start_background_noises()
            except Exception as e:
                self.local_logger.warning(f"Failed to restart noise manager: {e}")

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

        self.local_logger.info("[DEPLOY] Deploying Prometheus…")
        self.prometheus.deploy()

        # Set up fault injection infrastructure based on problem type
        # Only one can be active at /var/openebs/local at a time
        problem_name = self.problem.__class__.__name__

        if "LatentSectorError" in problem_name:
            print("Setting up dm-dust infrastructure for LSE fault injection...")
            self.dm_dust_manager.setup_openebs_dm_dust_infrastructure()
        elif "SilentDataCorruption" in problem_name:
            print("Setting up dm-flakey infrastructure for Silent Data Corruption fault injection...")
            self.dm_flakey_manager.setup_openebs_dm_flakey_infrastructure()

        self.logger.info(f"[ENV] Set up necessary components: metrics-server, Khaos, OpenEBS, Prometheus")

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

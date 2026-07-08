import asyncio
import concurrent.futures
import json
import logging
import shlex
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
from sregym.observer.jaeger import Jaeger
from sregym.observer.otel_collector import OtelCollector
from sregym.paths import CLUSTER_BASELINE_STATE_FILE
from sregym.service.apps.app_registry import AppRegistry
from sregym.service.cluster_state import ClusterStateManager
from sregym.service.dm_flakey_manager import DmFlakeyManager
from sregym.service.k8s_proxy import KubernetesAPIProxy
from sregym.service.khaos import KhaosController
from sregym.service.kubectl import KubeCtl
from sregym.service.mcp_server import MCPServer
from sregym.service.telemetry.loki import Loki
from sregym.service.telemetry.prometheus import Prometheus


@dataclass
class ConductorConfig:
    """Configuration for Conductor deployment options."""

    deploy_loki: bool = True
    enable_noise: bool = False


class Conductor:
    def __init__(self, config: ConductorConfig | None = None):
        self.config = config or ConductorConfig()

        # core services
        self.problems = ProblemRegistry()
        self.kubectl = KubeCtl()
        self.prometheus = Prometheus()
        self.jaeger = Jaeger()
        self.otel_collector = OtelCollector()
        self.loki = Loki()
        self.mcp_server = MCPServer()
        self.apps = AppRegistry()
        self.agent_name = None

        self.khaos = KhaosController(self.kubectl)
        self.dm_flakey_manager = DmFlakeyManager(self.kubectl)
        self.cluster_state = ClusterStateManager(self.kubectl)
        self._baseline_captured = False

        # Kubernetes API proxy to hide chaos engineering namespaces and load generators from agents
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
        self._submit_future = None  # Future for the executor running _submit_evaluate_and_advance

        self.tasklist = None
        self.logger = logging.getLogger("all.sregym.conductor")

        self.stage_sequence: list[dict] = []
        self.current_stage_index: int = 0
        self.waiting_for_agent: bool = False
        self._evaluating: bool = False  # True while a submission is being evaluated
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
        self._evaluating = False
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
        try:
            r = problem.diagnosis_oracle.evaluate(solution)
        except Exception as e:
            self.logger.exception("Diagnosis oracle raised; recording as failure to avoid a stuck stage.")
            r = {"success": False, "error": f"{type(e).__name__}: {e}"}
        r["submission"] = solution
        self.results["Diagnosis"] = r
        self.results["TTL"] = time.time() - self.execution_start_time
        self.logger.info(
            f"[EVAL] Diagnosis "
            f"{'Succeed' if self.results['Diagnosis'].get('success') else 'Failed'}\n "
            f"TTL: {self.results['TTL']}"
        )
        return r

    def _evaluate_mitigation(self, solution):
        """Evaluation logic for mitigation stage."""
        problem = self.current_problem
        # Currently mitigation_oracle.evaluate() does not take the agent solution directly.
        self.logger.info("Start Eval for Mitigation", extra={"sol": solution})
        try:
            r = problem.mitigation_oracle.evaluate()
        except Exception as e:
            self.logger.exception("Mitigation oracle raised; recording as failure to avoid a stuck stage.")
            r = {"success": False, "error": f"{type(e).__name__}: {e}"}
        self.results["Mitigation"] = r
        self.results["TTM"] = time.time() - self.execution_start_time
        self.logger.info(
            f"[EVAL] Mitigation "
            f"{'Succeed' if self.results['Mitigation'].get('success') else 'Failed'}\n "
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
            if self.config.enable_noise:
                try:
                    nm = get_noise_manager()
                    nm.set_stage(stage_name)
                except Exception as e:
                    self.logger.warning(f"Failed to set NoiseManager stage: {e}")
        else:
            # No more stages; finish the problem
            self._finish_problem()

    def _cleanup_sync(self):
        """
        Blocking cleanup operations (fault recovery, app teardown, reconciliation).
        Captures self.problem at entry so that start_problem() can safely replace
        self.problem/self.app for the next problem without affecting this cleanup.
        """
        # Snapshot the problem reference immediately so that any concurrent
        # replacement of self.problem by start_problem() does not affect this cleanup.
        problem = self.problem

        self.logger.info("[CLEANUP] Starting cleanup (fault recovery, undeploy, reconcile)")

        # Stop noises
        if self.config.enable_noise:
            try:
                nm = get_noise_manager()
                nm.stop()
                self.logger.info("[CLEANUP] NoiseManager stopped")
            except Exception as e:
                self.logger.warning(f"Failed to stop NoiseManager: {e}")

        # Recover fault using the captured problem reference
        if problem:
            self.logger.info("[CLEANUP] Recovering fault...")
            problem.recover_fault()
            self.logger.info("[CLEANUP] Fault recovered")

        # Undeploy app using the captured problem reference
        self.logger.info("[CLEANUP] Undeploying app...")
        if problem:
            problem.app.cleanup()
        self.logger.info("[CLEANUP] App undeployed")

        # Reconcile cluster state to baseline
        if self._baseline_captured:
            self.logger.info("[CLEANUP] Reconciling cluster state to baseline...")
            try:
                changes = self.cluster_state.reconcile_to_baseline()
                if any(v for v in changes.values() if v):
                    self.logger.info(f"Cluster state reconciliation changes: {changes}")
                self.logger.info("[CLEANUP] Cluster state reconciled")
            except Exception as e:
                self.logger.warning(f"Failed to reconcile cluster state: {e}")

        # Set to "done" after all cleanup is complete
        self.submission_stage = "done"
        self.logger.info("[CLEANUP] Cleanup complete, stage set to 'done'")

    def _finish_problem(self):
        """
        Runs problem teardown synchronously (fault recovery, app undeploy, cluster
        reconciliation) before returning.

        Idempotent: safe to call from multiple paths (submit flow via
        ``_advance_to_next_stage``, and ``main.py`` as a post-exit safety net).
        The first call runs cleanup; subsequent calls no-op. ``start_problem()``
        resets the stage to ``"setup"``, so the deploy-retry path still cleans up
        each failed attempt.
        """
        if self.submission_stage in ("done", "tearing_down"):
            self.logger.info(
                f"[STAGE] _finish_problem already ran/running (submission_stage={self.submission_stage!r}); skipping"
            )
            return
        self.logger.info("[STAGE] Done, starting teardown")
        self.submission_stage = "tearing_down"
        self._cleanup_sync()
        self.logger.info("[STAGE] Teardown complete")

    async def start_problem(self) -> StartProblemResult:
        """
        1) Provision infra & workload
        2) Initialize Act registry and execute initial GymActs and first AgentAct precondition

        Returns:
            StartProblemResult: Result status indicating success or skip reason
        """
        if self.problem_id is None:
            raise RuntimeError("Cannot start problem: problem_id is not set")

        # Wait for the previous problem's executor (evaluation + cleanup) to finish
        # before starting a new problem. _finish_problem() is called synchronously
        # from within _submit_evaluate_and_advance(), so awaiting the future here
        # guarantees that fault recovery, undeploy, and reconciliation are all done.
        if self._submit_future is not None and not self._submit_future.done():
            self.logger.info("[WAIT] Waiting for previous problem's cleanup to finish...")
            await asyncio.wrap_future(self._submit_future)
            self.logger.info("[WAIT] Previous problem's cleanup finished")
        self._submit_future = None

        self.execution_start_time = time.time()
        self.problem = self.problems.get_problem_instance(self.problem_id)
        self.app = self.problem.app
        self.detection_oracle = DetectionOracle(self.problem)
        self.results = {}

        self.dependency_check(["kubectl", "helm", "docker"])
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
        if self.config.enable_noise:
            try:
                nm = get_noise_manager()
                context = {
                    "namespace": self.app.namespace,
                    "app_name": self.app.name,
                    # We can add more info here if needed, e.g. service list
                }
                nm.set_problem_context(context)
                nm.start()
            except Exception as e:
                self.logger.warning(f"Failed to update NoiseManager context: {e}")

        # After deployment, advance to the first stage
        self._advance_to_next_stage(start_index=0)

        self.execution_start_time = time.time()  # Reset: measure agent time only

        if self.submission_stage and self.submission_stage != "done":
            self.logger.info(f"✅ Deployment complete. Ready for submission. Current stage is: {self.submission_stage}")
        else:
            self.logger.info(
                "✅ Deployment complete. No stages configured; problem will complete without agent submission."
            )
        return StartProblemResult.SUCCESS

    def _submit_evaluate_and_advance(self, sol, current_stage):
        """
        Blocking work for a submission: evaluate the oracle, advance stage, manage noise.
        Runs in a background thread so the HTTP response is not blocked.
        """
        stage_name: str = current_stage["name"]
        self.logger.info(f"Evaluating stage '{stage_name}'", extra={"sol": sol})

        # Stop noise before evaluation to ensure clean environment
        if self.config.enable_noise:
            try:
                nm = get_noise_manager()
                self.logger.info("Stopping noise manager before evaluation...")
                nm.stop()
            except Exception as e:
                self.logger.warning(f"Failed to stop noise manager: {e}")

        try:
            # Run the evaluation function for the current stage. The per-stage
            # _evaluate_* methods catch their own oracle exceptions and record
            # a failure result; this outer guard is defense in depth so the
            # stage always advances even if something above the oracle blows up.
            try:
                current_stage["evaluation"](sol)
            except Exception:
                self.logger.exception(
                    f"Stage '{stage_name}' evaluation raised unexpectedly; advancing anyway "
                    "so the conductor doesn't get stuck waiting on a dead stage."
                )
                self.results.setdefault(
                    stage_name.capitalize(), {"success": False, "error": "stage evaluation raised", "submission": sol}
                )
        finally:
            self._evaluating = False

        # After evaluation, advance to the next stage (if any)
        next_index = self.current_stage_index + 1
        self._advance_to_next_stage(start_index=next_index)

        # Restart noise if there are more stages AND not in teardown
        if self.config.enable_noise and self.submission_stage not in ("done", "tearing_down"):
            try:
                nm = get_noise_manager()
                self.logger.info("Restarting noise manager for next stage...")
                nm.start()
            except Exception as e:
                self.logger.warning(f"Failed to restart noise manager: {e}")

    async def submit(self, solution: str | None) -> dict:
        """
        Called by CLI or HTTP /submit.  Kicks off evaluation in the
        background and returns immediately.
        """
        sol = solution

        # If all tasks are already completed, simply return the final snapshot.
        if self.submission_stage == "done":
            self.logger.info("All tasks already completed; ignoring new submission.")
            return dict(self.results)

        # If teardown is in progress, return current results without evaluation
        if self.submission_stage == "tearing_down":
            self.logger.info("Teardown in progress; returning current results without evaluation.")
            return dict(self.results)

        if not self.stage_sequence:
            self.logger.warning("submit() called but no stages are configured; returning current results.")
            return dict(self.results)

        if not self.waiting_for_agent:
            if self._evaluating:
                self.logger.info(
                    "submit() called while evaluation is already in progress for "
                    f"stage '{self.submission_stage}'. Submission was already accepted."
                )
                return {"status": "ok", "message": "Submission already accepted; evaluation in progress."}
            self.logger.error(
                "submit() called when conductor is not waiting for a submission. "
                f"Current submission_stage={self.submission_stage}"
            )
            raise RuntimeError("Conductor is not currently waiting for an agent submission.")

        current_stage = self.stage_sequence[self.current_stage_index]

        # Mark that we're no longer waiting so duplicate submits are rejected
        self.waiting_for_agent = False
        self._evaluating = True

        # Run evaluation and stage advancement in an executor thread so the HTTP
        # response returns immediately.  Store the future so start_problem() can
        # await it and guarantee cleanup is fully done before the next problem starts.
        # Use concurrent.futures directly (not asyncio's run_in_executor) so the
        # future is loop-independent.  submit() is called from the uvicorn API thread
        # which has its own event loop; start_problem() runs in the main driver loop.
        # asyncio.wrap_future() in start_problem() binds the future to whichever loop
        # is running at await time, avoiding "Future attached to a different loop".
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self._submit_future = executor.submit(self._submit_evaluate_and_advance, sol, current_stage)
        executor.shutdown(wait=False)

        return {"status": "ok", "message": "Submission received"}

    @staticmethod
    def _q(value):
        return shlex.quote(str(value))

    def _fix_calico_route_reflector_label_drift(self):
        self.logger.info("[FIX] Calico route-reflector label drift leftovers if any")
        try:
            from sregym.conductor.problems.calico_route_reflector_label_drift import (
                CalicoRouteReflectorLabelDriftHotelReservation,
            )

            problem = CalicoRouteReflectorLabelDriftHotelReservation
            kubectl = KubeCtl()

            def kubectl_json(command):
                output = kubectl.exec_command(command)
                try:
                    return json.loads(output)
                except json.JSONDecodeError:
                    return None

            def has_problem_label(resource):
                if not resource:
                    return False
                labels = resource.get("metadata", {}).get("labels", {}) or {}
                return labels.get(problem.PROBLEM_LABEL_KEY) == problem.PROBLEM_LABEL_VALUE

            def bool_from_state(data, key):
                if not data or key not in data:
                    return None
                return json.loads(data[key])

            def list_from_state(data, key):
                if not data or key not in data:
                    return None
                return json.loads(data[key])

            nodes = (kubectl_json("kubectl get nodes -o json") or {}).get("items", [])
            marked_nodes = [
                node.get("metadata", {}).get("name")
                for node in nodes
                if (node.get("metadata", {}).get("annotations", {}) or {}).get(problem.NODE_MARKER_ANNOTATION) == "true"
            ]
            marked_nodes = [node for node in marked_nodes if node]

            bgppeer = kubectl_json(f"kubectl get bgppeer {self._q(problem.BGP_PEER_NAME)} -o json")
            bgppeers = (kubectl_json("kubectl get bgppeers -o json") or {}).get("items", [])
            bgp_config = kubectl_json("kubectl get bgpconfiguration default -o json")
            support_namespace = kubectl_json(f"kubectl get namespace {self._q(problem.PROBE_NAMESPACE)} -o json")
            state_configmap = kubectl_json(
                f"kubectl -n {self._q(problem.STATE_NAMESPACE)} get configmap "
                f"{self._q(problem.STATE_CONFIGMAP_NAME)} -o json"
            )
            state_data = (state_configmap or {}).get("data", {}) or {}

            bgppeer_labeled = has_problem_label(bgppeer)
            bgp_config_labeled = has_problem_label(bgp_config)
            support_namespace_labeled = has_problem_label(support_namespace)
            original_bgppeer_names = list_from_state(state_data, problem.STATE_BGP_PEERS_KEY)
            created_bgppeer_names = set()
            if original_bgppeer_names is not None:
                original_bgppeer_names = set(original_bgppeer_names)
                current_bgppeer_names = {
                    peer.get("metadata", {}).get("name") for peer in bgppeers if peer.get("metadata", {}).get("name")
                }
                created_bgppeer_names = current_bgppeer_names - original_bgppeer_names
            elif bgppeer_labeled:
                created_bgppeer_names.add(problem.BGP_PEER_NAME)
            residue_found = bool(
                marked_nodes
                or created_bgppeer_names
                or bgppeer_labeled
                or bgp_config_labeled
                or support_namespace_labeled
                or state_data
            )
            if not residue_found:
                return

            if support_namespace_labeled:
                kubectl.exec_command(f"kubectl delete namespace {self._q(problem.PROBE_NAMESPACE)} --ignore-not-found")
            for bgppeer_name in sorted(created_bgppeer_names):
                kubectl.exec_command(f"kubectl delete bgppeer {self._q(bgppeer_name)} --ignore-not-found")

            bgp_config_preexisted = bool_from_state(state_data, problem.STATE_CONFIG_PREEXISTED_KEY)
            original_bgp_configuration = state_data.get(problem.STATE_CONFIGURATION_KEY)
            if bgp_config_preexisted is True and original_bgp_configuration:
                kubectl.exec_command("kubectl apply -f -", input_data=original_bgp_configuration)
            elif bgp_config_preexisted is False or bgp_config_labeled:
                kubectl.exec_command("kubectl delete bgpconfiguration default --ignore-not-found")

            state_route_reflector_node = state_data.get(problem.STATE_PRIMARY_NODE_KEY)
            if state_route_reflector_node and state_route_reflector_node not in marked_nodes:
                marked_nodes.append(state_route_reflector_node)
            legacy_label_preexisted = bool_from_state(state_data, problem.STATE_NODE_LABEL_PREEXISTED_KEY)
            rr_annotation_preexisted = bool_from_state(state_data, problem.STATE_NODE_ANNOTATION_PREEXISTED_KEY)
            rr_annotation_value = state_data.get(problem.STATE_NODE_ANNOTATION_VALUE_KEY)

            for node_name in marked_nodes:
                quoted_node = self._q(node_name)
                if legacy_label_preexisted is True:
                    kubectl.exec_command(
                        f"kubectl label node {quoted_node} {self._q(f'{problem.LEGACY_MASTER_LABEL}=')} --overwrite"
                    )
                else:
                    kubectl.exec_command(
                        f"kubectl label node {quoted_node} {self._q(f'{problem.LEGACY_MASTER_LABEL}-')}"
                    )

                if rr_annotation_preexisted is True and rr_annotation_value:
                    kubectl.exec_command(
                        f"kubectl annotate node {quoted_node} "
                        f"{self._q(f'{problem.ROUTE_REFLECTOR_CLUSTER_ID_ANNOTATION}={rr_annotation_value}')} --overwrite"
                    )
                else:
                    kubectl.exec_command(
                        f"kubectl annotate node {quoted_node} "
                        f"{self._q(f'{problem.ROUTE_REFLECTOR_CLUSTER_ID_ANNOTATION}-')}"
                    )
                kubectl.exec_command(
                    f"kubectl annotate node {quoted_node} {self._q(f'{problem.NODE_MARKER_ANNOTATION}-')}"
                )

            kubectl.exec_command(
                f"kubectl -n {self._q(problem.STATE_NAMESPACE)} delete configmap "
                f"{self._q(problem.STATE_CONFIGMAP_NAME)} --ignore-not-found"
            )
            kubectl.exec_command("kubectl -n kube-system rollout restart ds/calico-node")
            kubectl.exec_command("kubectl -n kube-system rollout status ds/calico-node --timeout=180s")
        except Exception as e:
            self.logger.warning(f"Could not fix Calico route-reflector label drift state: {e}")

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

        self.logger.info("[FIX] KubeletEvictionThresholdMisconfig leftover if any")
        injector.recover_disk_pressure_all()
        # Delete Failed pods left by the eviction loop. Skip the app namespace
        # because undeploy_app() tears down the application namespace anyway.
        from sregym.conductor.problems.kubelet_eviction_threshold_misconfig import KubeletEvictionThresholdMisconfig

        self.kubectl.exec_command(
            f"kubectl delete pods --all-namespaces "
            f"--field-selector=status.phase=Failed,metadata.namespace!={KubeletEvictionThresholdMisconfig.NAMESPACE} "
            "--ignore-not-found=true"
        )

        self.logger.info("[FIX] Calico IPPool/strictAffinity leftover if any")
        try:
            from sregym.conductor.problems.pod_cidr_exhaustion_hotel_reservation import (
                PodCIDRExhaustionHotelReservation,
            )

            kubectl = KubeCtl()
            kubectl.exec_command(
                f"kubectl patch ippool {PodCIDRExhaustionHotelReservation.DEFAULT_POOL_NAME} --type=merge "
                '-p \'{"spec":{"disabled":false}}\''
            )
            kubectl.exec_command(
                'kubectl patch ipamconfig default --type=merge -p \'{"spec":{"strictAffinity":false}}\''
            )
            kubectl.exec_command(
                f"kubectl delete ippool {PodCIDRExhaustionHotelReservation.TINY_POOL_NAME} --ignore-not-found"
            )
            kubectl.exec_command(
                f"kubectl delete namespace {PodCIDRExhaustionHotelReservation.EXHAUST_NAMESPACE} --ignore-not-found"
            )
        except Exception as e:
            self.logger.warning(f"Could not fix Calico IPPool state: {e}")

        self._fix_calico_route_reflector_label_drift()

        self.logger.info("[FIX] Stale CoreDNS NXDOMAIN templates if any")
        injector = VirtualizationFaultInjector(namespace="kube-system")
        try:
            injector.recover_all_nxdomain_templates()
        except Exception as e:
            self.logger.error(f"Failed to recover CoreDNS NXDOMAIN templates: {e}")

        self.logger.info("[FIX] Leftover dm-flakey infrastructure if any")
        try:
            self.dm_flakey_manager.teardown_openebs_dm_flakey_infrastructure()
        except Exception as e:
            self.logger.warning(f"Could not teardown dm-flakey (Khaos may not be deployed yet): {e}")

        self.logger.info("[FIX] Clock drift leftover if any")
        try:
            injector = RemoteOSFaultInjector()
            injector.recover_clock_drift()
        except Exception as e:
            self.logger.warning(f"Could not fix leftover clock drift state: {e}")

        self.logger.info("Fix Kubernetes completed.")

    def deploy_app(self):
        """Kubectl + Prometheus + problem.app deployment."""
        problem = self.current_problem
        self.submission_stage = "setup"

        # Load or capture baseline state BEFORE any infrastructure deployment.
        # This captures the bare cluster state so reconciliation can clean up
        # everything added during a problem run (including infrastructure drift).
        if not self._baseline_captured:
            if self.cluster_state.load_baseline_state(CLUSTER_BASELINE_STATE_FILE):
                self.logger.info("[DEPLOY] Loaded persisted cluster baseline state")
            else:
                self.logger.info("[DEPLOY] No persisted baseline state found, capturing and saving...")
                self.cluster_state.save_baseline_state(CLUSTER_BASELINE_STATE_FILE)
            self._baseline_captured = True

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
        self._preflight_openebs_udev_mount()
        self.kubectl.exec_command("kubectl apply -f https://openebs.github.io/charts/openebs-operator.yaml")
        self.kubectl.exec_command(
            "kubectl patch storageclass openebs-hostpath "
            '-p \'{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}\''
        )
        self.kubectl.wait_for_ready("openebs")
        self._ensure_openebs_device_storageclass()

        self.logger.info("[DEPLOY] Deploying Prometheus…")
        self.prometheus.deploy()

        self.logger.info("[DEPLOY] Deploying Jaeger…")
        self.jaeger.deploy()

        self.logger.info("[DEPLOY] Deploying OTel Collector…")
        self.otel_collector.deploy()

        if self.config.deploy_loki:
            self.logger.info("[DEPLOY] Deploying Loki…")
            self.loki.deploy()
        else:
            self.logger.info("[DEPLOY] Skipping Loki deployment (external harness mode)")

        self.logger.info("[DEPLOY] Deploying MCP server…")
        self.mcp_server.deploy()

        self.logger.info("[ENV] Set up necessary components: metrics-server, Khaos, OpenEBS, Prometheus, Jaeger, Loki")

        # train-ticket pods need jaeger at startup; create ExternalName before deploy.
        # Other apps get it after deploy to avoid Helm ownership conflicts.
        is_train_ticket = problem.app.__class__.__name__ == "TrainTicket"

        # Composite apps span multiple namespaces; fall back to the single
        # `namespace` attribute for regular apps.
        app_namespaces = getattr(problem.app, "namespaces", None) or [problem.app.namespace]

        if is_train_ticket:
            for ns in app_namespaces:
                self.kubectl.exec_command(
                    f"kubectl create namespace {ns} --dry-run=client -o yaml | kubectl apply -f -"
                )
                self.jaeger.create_external_name_service(ns)

        self.logger.info("[DEPLOY] Deploying and starting workload")
        problem.app.deploy()
        self.logger.info(f"[ENV] Deploy application: {problem.app.name}")

        if not is_train_ticket:
            for ns in app_namespaces:
                self.jaeger.create_external_name_service(ns)

        problem.app.start_workload()
        self.logger.info("[ENV] Start workload")

    def undeploy_app(self):
        """Teardown problem.app and, if no other apps running, OpenEBS/Prometheus."""
        if self.problem:
            self.problem.app.cleanup()

    def _preflight_openebs_udev_mount(self) -> None:
        if shutil.which("docker") is None:
            self.logger.info("[DEPLOY] Docker is unavailable; skipping kind /run/udev preflight")
            return

        nodes_output = self.kubectl.exec_command(
            "kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{\"\\n\"}{end}'"
        )
        missing_nodes: list[str] = []

        for node in (line.strip() for line in nodes_output.splitlines()):
            if not node or " " in node:
                continue
            quoted_node = shlex.quote(node)
            result = self.kubectl.exec_command(
                f"docker inspect {quoted_node} >/dev/null 2>&1 "
                f"&& docker exec {quoted_node} sh -c 'test -d /run/udev && echo ok || echo missing' "
                "|| echo not-a-docker-node"
            ).strip()
            if result == "missing":
                missing_nodes.append(node)

        if missing_nodes:
            missing_list = ", ".join(missing_nodes)
            raise RuntimeError(
                "OpenEBS node-disk-manager requires /run/udev inside kind node containers. "
                f"Missing /run/udev on: {missing_list}. Create /run/udev on the host before kind cluster "
                "creation and mount hostPath /run/udev to containerPath /run/udev in the kind config."
            )

    def _ensure_openebs_device_storageclass(self) -> None:
        self.logger.info("[DEPLOY] Ensuring OpenEBS LocalPV-Device StorageClass…")
        existing = self.kubectl.exec_command(
            "kubectl get storageclass openebs-device -o jsonpath='{.provisioner}{\"\\n\"}{.volumeBindingMode}'"
        )
        if "not found" not in existing.lower() and "error from server" not in existing.lower():
            provisioner, _, volume_binding_mode = existing.partition("\n")
            if provisioner.strip() != "openebs.io/local":
                raise RuntimeError(
                    "StorageClass/openebs-device already exists with unexpected provisioner "
                    f"{provisioner.strip()!r}; expected 'openebs.io/local'."
                )
            if volume_binding_mode.strip() != "WaitForFirstConsumer":
                raise RuntimeError(
                    "StorageClass/openebs-device already exists with unexpected volumeBindingMode "
                    f"{volume_binding_mode.strip()!r}; expected 'WaitForFirstConsumer'."
                )
            self.logger.info("[DEPLOY] StorageClass/openebs-device already exists; leaving it unchanged")
            return

        device_sc_yaml = """apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: openebs-device
  annotations:
    openebs.io/cas-type: local
    cas.openebs.io/config: |
      - name: StorageType
        value: "device"
provisioner: openebs.io/local
volumeBindingMode: WaitForFirstConsumer
reclaimPolicy: Delete
"""
        result = self.kubectl.exec_command("kubectl apply -f -", input_data=device_sc_yaml)
        if "error from server" in result.lower() or "invalid" in result.lower():
            raise RuntimeError(f"Failed to create StorageClass/openebs-device: {result.strip()}")

    def get_deployed_apps(self):
        deployed_apps = []
        for app_name in self.apps.get_app_names():
            namespace = self.apps.get_app_metadata(app_name)["Namespace"]
            if self.kubectl.get_namespace_deployment_status(namespace):
                deployed_apps.append(app_name)

        return deployed_apps

import asyncio
import concurrent.futures
import json
import logging
import shlex
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from sregym.conductor.constants import StartProblemResult
from sregym.conductor.oracles.detection import DetectionOracle
from sregym.conductor.oracles.diagnosis_oracle import DiagnosisOracle
from sregym.conductor.problems.registry import ProblemRegistry
from sregym.conductor.submission import (
    SUBMISSION_STAGES,
    EvaluationInProgress,
    SubmissionAttemptClosed,
    SubmissionAttemptMismatch,
    SubmissionStage,
    SubmissionStageMismatch,
)
from sregym.conductor.utils import is_ordered_subset
from sregym.generators.fault.inject_remote_os import RemoteOSFaultInjector
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.generators.noise.manager import get_noise_manager
from sregym.observer.jaeger import Jaeger
from sregym.observer.otel_collector import OtelCollector
from sregym.paths import CLUSTER_BASELINE_STATE_FILE
from sregym.profile import is_svelte
from sregym.service.apps.app_registry import AppRegistry
from sregym.service.cluster_state import ClusterStateManager
from sregym.service.dm_flakey_manager import DmFlakeyManager
from sregym.service.internet_policy import InternetPolicy
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
    internet_policy: InternetPolicy = field(default_factory=InternetPolicy)
    k8s_proxy_listen_host: str = "127.0.0.1"
    block_workload_creation: bool = False


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
            listen_host=self.config.k8s_proxy_listen_host,
            block_workload_creation=self.config.block_workload_creation,
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
        self._submission_lock = threading.RLock()
        self._pending_submission_stages: dict[tuple[int, str], int] = {}
        self._submission_generation = 0
        self._aborted_submission_generations: set[int] = set()
        self._accepting_submissions = False
        self._attempt_closed = True

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
        self._accepting_submissions = False
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

        # Snapshot the healthy cluster before breaking it. The oracle is built
        # in Problem.__init__, which runs before deploy_app(), so this is the
        # first point at which the app actually exists. Also lets a mitigation
        # oracle ignore chronic pre-existing noise (e.g. ContainerCPUThrottling
        # from the astronomy-shop Grafana sidecar, SREGym#745) and grade only
        # the injected fault.
        mitigation_oracle = getattr(problem, "mitigation_oracle", None)
        if mitigation_oracle is not None:
            mitigation_oracle.capture_baseline()

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
        self.logger.info(
            f"[EVAL] Diagnosis {'Succeed' if r.get('success') else 'Failed'}\n "
            f"TTL: {time.time() - self.execution_start_time}"
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
        self.logger.info(
            f"[EVAL] Mitigation {'Succeed' if r.get('success') else 'Failed'}\n "
            f"TTM: {time.time() - self.execution_start_time}"
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
            self.logger.info("No stages configured; starting problem cleanup.")
            self.finish_problem_in_background()
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
            self._accepting_submissions = not self._attempt_closed
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
            self.finish_problem_in_background()

    def _cleanup_sync(self, generation: int | None = None):
        """
        Blocking cleanup operations (fault recovery, app teardown, reconciliation).
        Captures self.problem at entry so that start_problem() can safely replace
        self.problem/self.app for the next problem without affecting this cleanup.
        """
        # Snapshot the problem reference immediately so that any concurrent
        # replacement of self.problem by start_problem() does not affect this cleanup.
        problem = self.problem
        cleanup_generation = self._submission_generation if generation is None else generation
        cleanup_errors: list[str] = []

        def cleanup_was_abandoned() -> bool:
            with self._submission_lock:
                return (
                    cleanup_generation != self._submission_generation
                    or cleanup_generation in self._aborted_submission_generations
                )

        def stop_late_cleanup() -> bool:
            if not cleanup_was_abandoned():
                return False
            self.logger.warning(
                "Stopping late cleanup work for aborted or replaced attempt generation %s",
                cleanup_generation,
            )
            return True

        self.logger.info("[CLEANUP] Starting cleanup (fault recovery, undeploy, reconcile)")

        # Stop noises
        if self.config.enable_noise:
            try:
                nm = get_noise_manager()
                nm.stop()
                self.logger.info("[CLEANUP] NoiseManager stopped")
            except Exception as e:
                self.logger.warning(f"Failed to stop NoiseManager: {e}")

        if stop_late_cleanup():
            return

        # Recover fault using the captured problem reference
        if problem:
            self.logger.info("[CLEANUP] Recovering fault...")
            try:
                problem.recover_fault()
                self.logger.info("[CLEANUP] Fault recovered")
            except Exception as exc:
                self.logger.exception("[CLEANUP] Fault recovery failed")
                cleanup_errors.append(f"recover_fault: {type(exc).__name__}: {exc}")

        if stop_late_cleanup():
            return

        # Undeploy app using the captured problem reference
        self.logger.info("[CLEANUP] Undeploying app...")
        if problem:
            try:
                problem.app.cleanup()
                self.logger.info("[CLEANUP] App undeployed")
            except Exception as exc:
                self.logger.exception("[CLEANUP] App cleanup failed")
                cleanup_errors.append(f"app_cleanup: {type(exc).__name__}: {exc}")

        if stop_late_cleanup():
            return

        # Reconcile cluster state to baseline
        if self._baseline_captured:
            self.logger.info("[CLEANUP] Reconciling cluster state to baseline...")
            try:
                changes = self.cluster_state.reconcile_to_baseline()
                if any(v for v in changes.values() if v):
                    self.logger.info(f"Cluster state reconciliation changes: {changes}")
                self.logger.info("[CLEANUP] Cluster state reconciled")
            except Exception as e:
                self.logger.exception("[CLEANUP] Failed to reconcile cluster state")
                cleanup_errors.append(f"cluster_reconcile: {type(e).__name__}: {e}")

        if stop_late_cleanup():
            return

        # Set to "done" after all cleanup is complete.
        with self._submission_lock:
            if cleanup_was_abandoned():
                self.logger.warning(
                    "Ignoring late cleanup state for aborted or replaced attempt generation %s",
                    cleanup_generation,
                )
                return
            if cleanup_errors:
                self.results["cleanup_failed"] = True
                self.results["cleanup_error"] = "; ".join(cleanup_errors)
            self.submission_stage = "done"
            self._accepting_submissions = False
            self._attempt_closed = True
        self.logger.info("[CLEANUP] Cleanup complete, stage set to 'done'")

    def _finish_problem(self, generation: int | None = None):
        """
        Runs problem teardown synchronously (fault recovery, app undeploy, cluster
        reconciliation) before returning.

        Idempotent: safe to call from multiple paths (submit flow via
        ``_advance_to_next_stage``, and ``main.py`` as a post-exit safety net).
        The first call runs cleanup; subsequent calls no-op. ``start_problem()``
        resets the stage to ``"setup"``, so the deploy-retry path still cleans up
        each failed attempt.
        """
        cleanup_generation = self._submission_generation if generation is None else generation
        with self._submission_lock:
            if (
                cleanup_generation != self._submission_generation
                or cleanup_generation in self._aborted_submission_generations
            ):
                self.logger.warning(
                    "Skipping cleanup transition for aborted or replaced attempt generation %s",
                    cleanup_generation,
                )
                return
            if self.submission_stage in ("done", "tearing_down"):
                self.logger.info(
                    f"[STAGE] _finish_problem already ran/running (submission_stage={self.submission_stage!r}); skipping"
                )
                return
            self.logger.info("[STAGE] Done, starting teardown")
            self._accepting_submissions = False
            self._attempt_closed = True
            self.submission_stage = "tearing_down"
        self._cleanup_sync(cleanup_generation)
        self.logger.info("[STAGE] Teardown complete")

    def finish_problem_in_background(self) -> concurrent.futures.Future:
        """Return running teardown or start it so the driver can enforce a deadline."""
        with self._submission_lock:
            if self._submit_future is not None:
                if self.submission_stage in ("tearing_down", "done"):
                    return self._submit_future
                raise RuntimeError("Cannot start cleanup while submission work is still active.")
            if self.submission_stage == "tearing_down":
                raise RuntimeError(
                    "Cleanup stopped unexpectedly while tearing down; it cannot be treated as complete or retried safely."
                )
            generation = self._submission_generation
            future: concurrent.futures.Future = concurrent.futures.Future()

            def run_cleanup() -> None:
                if not future.set_running_or_notify_cancel():
                    return
                try:
                    self._finish_problem(generation)
                except BaseException as exc:
                    if isinstance(exc, Exception):
                        future.set_exception(exc)
                    else:
                        future.set_exception(RuntimeError(f"Cleanup terminated with {type(exc).__name__}: {exc}"))
                else:
                    future.set_result(None)

            self._submit_future = future
            threading.Thread(
                target=run_cleanup,
                name=f"sregym-cleanup-{generation}",
                daemon=True,
            ).start()
            return future

    async def start_problem(self) -> StartProblemResult:
        """
        1) Provision infra & workload
        2) Initialize Act registry and execute initial GymActs and first AgentAct precondition

        Returns:
            StartProblemResult: Result status indicating success or skip reason
        """
        if self.problem_id is None:
            raise RuntimeError("Cannot start problem: problem_id is not set")

        with self._submission_lock:
            if self._submission_generation in self._aborted_submission_generations:
                raise RuntimeError(
                    "This Conductor abandoned a stuck evaluator and cannot be reused. "
                    "Start a new benchmark process so late external side effects cannot overlap another attempt."
                )

        # Wait for every accepted evaluation and any API request that is waiting
        # for the next stage. This prevents a late request from one attempt from
        # being submitted against the next problem.
        # Close the old attempt before taking the drain snapshot. A request
        # that was already registered can finish, but no later request can
        # cross this boundary into the new attempt.
        self.close_submissions()
        if self._submit_future is not None or self._pending_submission_stages:
            self.logger.info("[WAIT] Waiting for previous problem's submission work to finish...")
            await self.wait_for_submission_work(timeout=None)
            self.logger.info("[WAIT] Previous problem's submission work finished")
        with self._submission_lock:
            self._submit_future = None
            self._submission_generation += 1
            self._accepting_submissions = False
            self._attempt_closed = False
            self.submission_stage = "setup"
            self.waiting_for_agent = False
            self._evaluating = False
            self.problem = None
            self.app = None
            self.results = {}

        self.execution_start_time = time.time()
        self.problem = self.problems.get_problem_instance(self.problem_id)
        self.app = self.problem.app
        self.detection_oracle = DetectionOracle(self.problem)

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

    def _submit_evaluate_and_advance(self, sol, current_stage, generation: int):
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

        outcome = None
        # Run the evaluation function for the current stage. The per-stage
        # _evaluate_* methods catch their own oracle exceptions; this outer
        # guard is defense in depth so an ordinary evaluation error still
        # produces a failed stage result.
        try:
            outcome = current_stage["evaluation"](sol)
        except Exception:
            self.logger.exception(
                f"Stage '{stage_name}' evaluation raised unexpectedly; advancing anyway "
                "so the conductor doesn't get stuck waiting on a dead stage."
            )
            outcome = {"success": False, "error": "stage evaluation raised", "submission": sol}

        next_index = self.current_stage_index + 1
        next_stage_name: str | None = None
        with self._submission_lock:
            if generation != self._submission_generation or generation in self._aborted_submission_generations:
                self.logger.warning(
                    "Discarding %s result because attempt generation %s was aborted or replaced",
                    stage_name,
                    generation,
                )
                return

            if outcome is not None:
                self.results[stage_name.capitalize()] = outcome
                timing_key = "TTL" if stage_name == "diagnosis" else "TTM"
                self.results[timing_key] = time.time() - self.execution_start_time

            if next_index < len(self.stage_sequence):
                next_stage_name = self.stage_sequence[next_index]["name"]

        if next_stage_name is not None:
            # Keep the old stage marked busy while noise is restarted. The
            # API can wait for the next stage, but cannot submit into a
            # partially completed transition. Do not hold the submission lock
            # across NoiseManager I/O because a stuck noise backend must not
            # prevent the driver from closing or abandoning the attempt.
            if self.config.enable_noise:
                nm = None
                try:
                    nm = get_noise_manager()
                    self.logger.info("Restarting noise manager for next stage...")
                    nm.start()
                    with self._submission_lock:
                        transition_aborted = (
                            generation != self._submission_generation
                            or generation in self._aborted_submission_generations
                        )
                    if not transition_aborted:
                        nm.set_stage(next_stage_name)
                except Exception as e:
                    self.logger.warning(f"Failed to restart NoiseManager: {e}")

                # A slow start/set_stage can finish after the driver abandons
                # the evaluator and performs its global noise stop. Stop again
                # so this late worker cannot leave noise running.
                with self._submission_lock:
                    transition_aborted = (
                        generation != self._submission_generation or generation in self._aborted_submission_generations
                    )
                if transition_aborted and nm is not None:
                    try:
                        nm.stop()
                    except Exception as e:
                        self.logger.warning(f"Failed to stop late NoiseManager restart: {e}")
                    return

            with self._submission_lock:
                if generation != self._submission_generation or generation in self._aborted_submission_generations:
                    self.logger.warning(
                        "Skipping %s stage transition because attempt generation %s was aborted or replaced",
                        next_stage_name,
                        generation,
                    )
                    return
                self.current_stage_index = next_index
                self.submission_stage = next_stage_name
                self.waiting_for_agent = True
                self._evaluating = False
                self._accepting_submissions = not self._attempt_closed
                self.logger.info(f"[STAGE] Go to stage {self.submission_stage}")
            return

        if next_index >= len(self.stage_sequence):
            # Keep the accepted evaluation marked as active until teardown
            # finishes. A concurrent submit observes ``tearing_down`` instead
            # of a brief, invalid "idle mitigation" state.
            try:
                self._finish_problem(generation)
            finally:
                with self._submission_lock:
                    if generation == self._submission_generation:
                        self._evaluating = False

    async def submit(
        self,
        solution: str | None,
        *,
        expected_stage: str | None = None,
        expected_generation: int | None = None,
    ) -> dict:
        """
        Called by CLI or HTTP /submit.  Kicks off evaluation in the
        background and returns immediately.
        """
        sol = solution

        with self._submission_lock:
            if expected_generation is not None and expected_generation != self._submission_generation:
                raise SubmissionAttemptMismatch(expected_generation, self._submission_generation)

            registered_request = (
                expected_generation is not None
                and self._pending_submission_stages.get((expected_generation, expected_stage or ""), 0) > 0
            )
            if not self._accepting_submissions and not registered_request:
                raise SubmissionAttemptClosed("This attempt no longer accepts new submissions.")

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

            if expected_stage is not None and self.submission_stage != expected_stage:
                raise SubmissionStageMismatch(expected_stage, self.submission_stage)

            if not self.waiting_for_agent:
                if self._evaluating:
                    self.logger.info(
                        f"submit() called while evaluation is already in progress for stage '{self.submission_stage}'."
                    )
                    raise EvaluationInProgress(self.submission_stage)
                self.logger.error(
                    "submit() called when conductor is not waiting for a submission. "
                    f"Current submission_stage={self.submission_stage}"
                )
                raise RuntimeError("Conductor is not currently waiting for an agent submission.")

            current_stage = self.stage_sequence[self.current_stage_index]
            accepted_stage: str = current_stage["name"]

            # Mark that we're no longer waiting so duplicate submits are rejected.
            self.waiting_for_agent = False
            self._evaluating = True

            # Run evaluation and stage advancement in an executor thread so the HTTP
            # response returns immediately. Store the future so start_problem() can
            # await it and guarantee cleanup is fully done before the next problem starts.
            generation = self._submission_generation
            future: concurrent.futures.Future = concurrent.futures.Future()

            def run_evaluation() -> None:
                if not future.set_running_or_notify_cancel():
                    return
                try:
                    result = self._submit_evaluate_and_advance(sol, current_stage, generation)
                except BaseException as exc:
                    if isinstance(exc, Exception):
                        future.set_exception(exc)
                    else:
                        future.set_exception(RuntimeError(f"Evaluator terminated with {type(exc).__name__}: {exc}"))
                else:
                    future.set_result(result)

            self._submit_future = future
            threading.Thread(
                target=run_evaluation,
                name=f"sregym-evaluate-{generation}-{accepted_stage}",
                daemon=True,
            ).start()

        return {"status": "accepted", "message": "Submission received", "stage": accepted_stage}

    def _register_pending_submission_locked(self, stage: str) -> int:
        if self._attempt_closed or not self._accepting_submissions:
            raise SubmissionAttemptClosed("This attempt no longer accepts new submissions.")
        generation = self._submission_generation
        key = (generation, stage)
        self._pending_submission_stages[key] = self._pending_submission_stages.get(key, 0) + 1
        return generation

    def register_submission_request(
        self,
        solution: str,
        stage: SubmissionStage | None = None,
    ) -> tuple[SubmissionStage, int]:
        """Resolve and register one request atomically.

        Legacy clients omit the stage. Preserve the current-stage behavior and
        recognize an empty request during diagnosis grading as early mitigation.
        """
        with self._submission_lock:
            requested_stage = stage
            if requested_stage is None:
                if self.submission_stage == "mitigation":
                    requested_stage = "mitigation"
                elif self.submission_stage == "diagnosis":
                    requested_stage = "mitigation" if self._evaluating and solution == "" else "diagnosis"
                else:
                    requested_stage = "mitigation" if solution == "" else "diagnosis"
            if requested_stage not in SUBMISSION_STAGES:
                raise ValueError(f"Unknown submission stage: {requested_stage!r}")
            generation = self._register_pending_submission_locked(requested_stage)
            return requested_stage, generation

    def register_pending_submission(self, stage: str) -> int:
        """Track a known-stage request and return its attempt generation."""
        with self._submission_lock:
            return self._register_pending_submission_locked(stage)

    def submission_state(self) -> tuple[str | None, bool, int]:
        """Return the agent-facing stage, evaluation flag, and attempt generation atomically."""
        with self._submission_lock:
            return self.submission_stage, self._evaluating, self._submission_generation

    def unregister_pending_submission(self, stage: str, generation: int) -> None:
        """Stop tracking an API request after it is accepted or rejected."""
        with self._submission_lock:
            key = (generation, stage)
            remaining = self._pending_submission_stages.get(key, 0) - 1
            if remaining > 0:
                self._pending_submission_stages[key] = remaining
            else:
                self._pending_submission_stages.pop(key, None)

    def close_submissions(self) -> bool:
        """Atomically reject new requests and report whether registered work remains."""
        with self._submission_lock:
            self._accepting_submissions = False
            self._attempt_closed = True
            return self._submit_future is not None or bool(self._pending_submission_stages)

    def abandon_submission_work(self) -> None:
        """Detach a stuck evaluator and prevent it from mutating this or a later attempt."""
        with self._submission_lock:
            generation = self._submission_generation
            self._aborted_submission_generations.add(generation)
            self._attempt_closed = True
            self._accepting_submissions = False
            self.waiting_for_agent = False
            self._evaluating = False
            self.submission_stage = "aborted"
            self._submit_future = None

    async def wait_for_submission_evaluations(self, timeout: float | None) -> None:
        """Wait for accepted stage requests and grading, but not teardown.

        Give each accepted stage evaluation a fresh deadline. An early
        mitigation request can wait behind diagnosis grading. Each will get
        their own deadlines.

        The final stage worker performs teardown in the same future. Return as
        soon as that worker enters ``tearing_down`` so the driver can apply a
        separate cleanup deadline without allowing attempts to overlap.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout if timeout is not None else None
        observed_future: concurrent.futures.Future | None = None

        while True:
            with self._submission_lock:
                stage = self.submission_stage
                future = self._submit_future
                pending = bool(self._pending_submission_stages)

            if future is not None and future is not observed_future:
                observed_future = future
                deadline = loop.time() + timeout if timeout is not None else None

            if future is not None and future.done():
                try:
                    future.result()
                finally:
                    with self._submission_lock:
                        if self._submit_future is future:
                            self._submit_future = None
                continue

            if stage in {"tearing_down", "done"}:
                return

            if future is None and not pending:
                return

            if deadline is not None:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError
                await asyncio.sleep(min(0.05, remaining))
            else:
                await asyncio.sleep(0.05)

    async def wait_for_submission_work(self, timeout: float | None) -> None:
        """Wait for all accepted evaluations and registered stage requests.

        An early mitigation request can be waiting while diagnosis is graded.
        When diagnosis finishes, that request creates a new evaluation future.
        Looping until both sources are idle prevents cleanup between those two
        evaluations.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout if timeout is not None else None

        while True:
            with self._submission_lock:
                future = self._submit_future
                pending = bool(self._pending_submission_stages)

            if future is not None:
                try:
                    if future.done():
                        future.result()
                    elif deadline is None:
                        await asyncio.shield(asyncio.wrap_future(future))
                    else:
                        remaining = deadline - loop.time()
                        if remaining <= 0:
                            raise TimeoutError
                        await asyncio.wait_for(
                            asyncio.shield(asyncio.wrap_future(future)),
                            timeout=remaining,
                        )
                finally:
                    if future.done():
                        with self._submission_lock:
                            if self._submit_future is future:
                                self._submit_future = None
                continue

            if pending:
                if deadline is not None and loop.time() >= deadline:
                    raise TimeoutError
                await asyncio.sleep(0.05)
                continue

            return

    def missing_submission_stages(self) -> list[str]:
        """Return configured stages that did not produce an evaluation result."""
        return [stage["name"] for stage in self.stage_sequence if stage["name"].capitalize() not in self.results]

    def record_incomplete_attempt(
        self,
        reason: str,
        *,
        agent_return_code: int | None = None,
    ) -> None:
        """Record why an attempt ended without all configured stage results."""
        missing = self.missing_submission_stages()
        current_stage = self.submission_stage
        if current_stage not in {"diagnosis", "mitigation", "tearing_down"}:
            current_stage = missing[0] if missing else "run"

        self.results["run_status"] = "incomplete"
        self.results["incomplete_reason"] = reason
        self.results["incomplete_stage"] = current_stage
        self.results["missing_stages"] = ",".join(missing)
        if agent_return_code is not None:
            self.results["agent_return_code"] = agent_return_code

    def finalize_attempt_status(self) -> str:
        """Set an explicit complete or incomplete status before results are written."""
        if self.results.get("run_status") == "incomplete":
            return "incomplete"
        if self.results.get("cleanup_failed"):
            self.record_incomplete_attempt("cleanup_failed")
            return "incomplete"
        if self.missing_submission_stages():
            self.record_incomplete_attempt("missing_stage_results")
        else:
            self.results["run_status"] = "complete"
        return self.results["run_status"]

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
        injector.recover_daemon_set_image_replacement(daemon_set_name="kube-proxy")

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

        self.logger.info("[FIX] NightlyRebalanceOOM kube-system actor leftover if any")
        try:
            from sregym.conductor.problems.nightly_rebalance_oom import NightlyRebalanceOOM

            NightlyRebalanceOOM.cleanup_leftover_actor()
        except Exception as e:
            self.logger.warning(f"Could not clean up NightlyRebalanceOOM actor: {e}")

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
        # metrics-server lives in kube-system, which is protected from
        # reconciliation, so it survives between problems. Re-applying it fetches
        # the manifest from GitHub every problem and rolls the deployment for no
        # gain. The apply and the patch must be skipped together: the patch is an
        # `add` to the args list, and running it without the apply that resets
        # those args would append duplicates on every problem.
        if self._metrics_server_configured():
            self.logger.info("[DEPLOY] metrics-server already present and patched; skipping re-apply")
        else:
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
        svelte = is_svelte()
        # `openebs` is protected from reconciliation, so it persists across
        # problems and the operator manifest only needs fetching once.
        if self._openebs_ready(svelte):
            self.logger.info("[DEPLOY] OpenEBS already deployed; skipping operator re-apply")
        else:
            if not svelte:
                # The udev preflight exists solely for the node-disk-manager,
                # which svelte does not deploy.
                self._preflight_openebs_udev_mount()
            self.kubectl.exec_command("kubectl apply -f https://openebs.github.io/charts/openebs-operator.yaml")
            self.kubectl.exec_command(
                "kubectl patch storageclass openebs-hostpath "
                '-p \'{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}\''
            )
        # Idempotent and cheap; also covers an `openebs` left over from an
        # earlier full-profile run on the same cluster.
        if svelte:
            self._trim_openebs_ndm()
        self.kubectl.wait_for_ready("openebs")
        if not svelte:
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

        if problem.run_default_workload:
            problem.app.start_workload()
            self.logger.info("[ENV] Start workload")
        else:
            self.logger.info("[ENV] Default application workload disabled for this problem")

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

    _METRICS_SERVER_ARGS = ("--kubelet-insecure-tls", "--kubelet-preferred-address-types=InternalIP")

    # components.yaml creates this alongside the Deployment. It grants
    # metrics-server the ability to create subjectaccessreviews; without it the
    # aggregated API returns 500 and every `kubectl top` fails.
    _METRICS_SERVER_BINDING = "metrics-server:system:auth-delegator"

    def _metrics_server_configured(self) -> bool:
        """True if metrics-server is deployed, patched, *and* still has its RBAC.

        Checking more than existence matters because the Deployment and its
        cluster-scoped RBAC have different lifetimes: the Deployment sits in the
        protected kube-system namespace, while the ClusterRoleBinding is
        cluster-scoped. Accepting a Deployment whose binding has been reconciled
        away would leave metrics-server permanently broken instead of repairing
        it, so verify both and re-apply if either is missing.
        """
        args = self.kubectl.exec_command(
            "kubectl -n kube-system get deployment metrics-server "
            "-o jsonpath='{.spec.template.spec.containers[0].args}' --ignore-not-found"
        )
        if not args or not args.strip():
            return False
        if not all(arg in args for arg in self._METRICS_SERVER_ARGS):
            return False

        binding = self.kubectl.exec_command(
            f"kubectl get clusterrolebinding {self._METRICS_SERVER_BINDING} -o name --ignore-not-found"
        )
        if not binding or not binding.strip():
            self.logger.info("[DEPLOY] metrics-server RBAC missing; re-applying components.yaml")
            return False
        return True

    def _openebs_ready(self, svelte: bool) -> bool:
        """True if the OpenEBS tier already matches what this profile wants.

        Under `full` that includes the node-disk-manager, so that a
        partially-present `openebs` namespace is repaired by re-applying the
        operator manifest rather than silently accepted. Checking it also matters
        because `openebs` persists between problems: a cluster whose last run was
        `svelte` has had NDM removed, and accepting the provisioner alone would
        leave `openebs-device` permanently unbacked after switching back.
        """
        out = self.kubectl.exec_command(
            "kubectl -n openebs get deployment openebs-localpv-provisioner "
            "-o jsonpath='{.status.readyReplicas}' --ignore-not-found"
        )
        try:
            if int(out.strip()) < 1:
                return False
        except (ValueError, AttributeError):
            return False

        if svelte:
            return True

        ndm = self.kubectl.exec_command("kubectl -n openebs get daemonset openebs-ndm -o name --ignore-not-found")
        if not ndm or not ndm.strip():
            self.logger.info("[DEPLOY] OpenEBS node-disk-manager missing; re-applying operator manifest")
            return False
        return True

    # openebs-operator.yaml bundles the node-disk-manager alongside the LocalPV
    # provisioner. NDM exists to discover block devices and back the
    # `openebs-device` StorageClass; SREGym only ever provisions through
    # `openebs-hostpath`, which needs the provisioner alone. Measured on one
    # problem, these seven pods cost 603m CPU (13% of the cluster) scanning
    # disks nothing claims.
    _OPENEBS_NDM_WORKLOADS = (
        ("daemonset", "openebs-ndm"),
        ("daemonset", "openebs-ndm-node-exporter"),
        ("deployment", "openebs-ndm-operator"),
        ("deployment", "openebs-ndm-cluster-exporter"),
    )

    def _trim_openebs_ndm(self) -> None:
        """Remove the node-disk-manager workloads left by openebs-operator.yaml.

        Deleting rather than scaling: the operator manifest is a plain apply with
        no controller to recreate them, and re-applying it recreates them for this
        method to remove again.
        """
        self.logger.info("[svelte] Removing OpenEBS node-disk-manager (openebs-hostpath does not use it)")
        for kind, name in self._OPENEBS_NDM_WORKLOADS:
            self.kubectl.exec_command(f"kubectl delete {kind} {name} -n openebs --ignore-not-found")

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

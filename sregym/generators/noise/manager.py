"""
NoiseManager — schedules transient Chaos Mesh experiments to simulate
real-world system noise (CI/CD churn, transient network blips, etc.).

No MCP coupling: noise is injected as real Kubernetes CRDs, not by
intercepting tool responses.
"""

import contextlib
import copy
import logging
import os
import random
import tempfile
import threading
import time
from typing import Any

import yaml

from sregym.generators.noise.catalog import EXPERIMENT_CATALOG
from sregym.service.kubectl import KubeCtl

logger = logging.getLogger(__name__)

# ── Defaults ──────────────────────────────────────────────────────────
CHAOS_NAMESPACE = "chaos-mesh"
MAX_CONCURRENT = 2  # experiments per injection cycle
DURATION = 120  # seconds each experiment lives
COOLDOWN = 300  # seconds between injection cycles


class NoiseManager:
    """Singleton that manages Chaos Mesh noise experiments."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True

        self.kubectl = KubeCtl()
        self.running = False
        self.current_stage: str | None = None
        self.target_namespace: str | None = None
        self.active_experiments: list[dict[str, str]] = []
        self._background_thread: threading.Thread | None = None
        self._last_injection_time: float = 0
        self._lock = threading.Lock()
        self._chaos_mesh_ready = False

    # ── Context from Conductor ────────────────────────────────────────

    def set_stage(self, stage: str):
        self.current_stage = stage
        logger.info(f"Noise stage set to: {stage}")

    def set_problem_context(self, context: dict[str, Any]):
        self.target_namespace = context.get("namespace")
        logger.info(f"Noise target namespace: {self.target_namespace}")

    # ── Lifecycle ─────────────────────────────────────────────────────

    def start(self):
        """Start the background noise injection loop."""
        if self.running:
            return
        self._ensure_chaos_mesh_installed()
        if not self._chaos_mesh_ready:
            logger.warning("Chaos Mesh is not ready; noise will not be injected.")
            return
        self.running = True
        self._background_thread = threading.Thread(target=self._background_loop, daemon=True)
        self._background_thread.start()
        logger.info("Noise injection started.")

    def stop(self):
        """Stop the background loop and clean up all active experiments."""
        self.running = False
        if self._background_thread:
            self._background_thread.join(timeout=5)
            self._background_thread = None
        self._cleanup_experiments()
        # Strip finalizers from any remaining chaos-mesh CRs so the namespace
        # can terminate cleanly when reconcile_to_baseline deletes it.
        self._force_remove_all_chaos_resources()
        self._last_injection_time = 0
        logger.info("Noise injection stopped.")

    # ── Background loop ───────────────────────────────────────────────

    def _background_loop(self):
        while self.running:
            try:
                self._maybe_inject()
            except Exception as e:
                logger.error(f"Error in noise background loop: {e}")
            time.sleep(5)

    def _maybe_inject(self):
        if not self.target_namespace:
            return

        now = time.time()
        if now - self._last_injection_time < COOLDOWN:
            return

        n = min(MAX_CONCURRENT, len(EXPERIMENT_CATALOG))
        selected = random.sample(EXPERIMENT_CATALOG, n)

        for template in selected:
            self._apply_experiment(template)

        self._last_injection_time = now

    # ── Experiment application ────────────────────────────────────────

    def _apply_experiment(self, template: dict):
        spec = copy.deepcopy(template["spec"])
        duration_str = f"{DURATION}s"
        self._format_placeholders(spec, self.target_namespace or "default", duration_str)

        timestamp = int(time.time())
        rand_suffix = random.randint(100, 999)
        name = f"noise-{template['name']}-{timestamp}-{rand_suffix}"
        kind = template["kind"]

        crd = {
            "apiVersion": "chaos-mesh.org/v1alpha1",
            "kind": kind,
            "metadata": {
                "name": name,
                "namespace": CHAOS_NAMESPACE,
            },
            "spec": spec,
        }

        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as tmp:
                yaml.dump(crd, tmp)
                tmp_path = tmp.name

            out = self.kubectl.exec_command(f"kubectl apply -f {tmp_path}")
            os.remove(tmp_path)
            logger.info(f"Applied noise experiment {name}: {out}")

            with self._lock:
                self.active_experiments.append({"name": name, "kind": kind})
        except Exception as e:
            logger.error(f"Failed to apply noise experiment {name}: {e}")

    @staticmethod
    def _format_placeholders(d: dict, target_namespace: str, duration: str):
        """Recursively replace {target_namespace} and {duration} in a spec dict."""
        for k, v in d.items():
            if isinstance(v, dict):
                NoiseManager._format_placeholders(v, target_namespace, duration)
            elif isinstance(v, list):
                for i, item in enumerate(v):
                    if isinstance(item, str):
                        d[k][i] = item.format(target_namespace=target_namespace, duration=duration)
                    elif isinstance(item, dict):
                        NoiseManager._format_placeholders(item, target_namespace, duration)
            elif isinstance(v, str):
                d[k] = v.format(target_namespace=target_namespace, duration=duration)

    # ── Cleanup ───────────────────────────────────────────────────────

    def _cleanup_experiments(self):
        with self._lock:
            for exp in self.active_experiments:
                try:
                    cmd = f"kubectl delete {exp['kind']} {exp['name']} -n {CHAOS_NAMESPACE} --ignore-not-found"
                    self.kubectl.exec_command(cmd)
                    logger.info(f"Cleaned up noise experiment {exp['name']}")
                except Exception as e:
                    logger.error(f"Failed to clean up noise experiment {exp['name']}: {e}")
            self.active_experiments.clear()

    def _force_remove_all_chaos_resources(self):
        """Remove finalizers from all chaos-mesh CRs so the namespace can terminate cleanly.

        When the chaos-mesh controller is gone (or being deleted), CRs with
        finalizers block namespace deletion indefinitely.  This method patches
        the finalizers away for every CR of every chaos-mesh CRD, then deletes
        the CRDs themselves.
        """
        try:
            crd_output = self.kubectl.exec_command("kubectl get crd -o name 2>/dev/null | grep chaos-mesh.org || true")
        except Exception:
            return

        if not crd_output or "chaos-mesh.org" not in crd_output:
            return

        crd_names = [
            line.removeprefix("customresourcedefinition.apiextensions.k8s.io/")
            for line in crd_output.strip().splitlines()
            if line.strip()
        ]

        for crd in crd_names:
            # The resource plural name is the first segment of the CRD name
            # e.g.  "networkchaos.chaos-mesh.org" → "networkchaos"
            resource = crd.split(".")[0]
            try:
                items = self.kubectl.exec_command(
                    f"kubectl get {resource}.chaos-mesh.org --all-namespaces "
                    f"-o jsonpath='{{range .items}}{{.metadata.namespace}}/{{.metadata.name}} {{end}}' "
                    f"2>/dev/null || true"
                )
            except Exception:
                continue

            for item in (items or "").split():
                item = item.strip()
                if not item or "/" not in item:
                    continue
                ns, name = item.split("/", 1)
                with contextlib.suppress(Exception):
                    self.kubectl.exec_command(
                        f"kubectl patch {resource}.chaos-mesh.org {name} -n {ns} "
                        f'--type merge -p \'{{"metadata":{{"finalizers":[]}}}}\' '
                        f"2>/dev/null || true"
                    )

        # Now delete the CRDs (should return quickly with finalizers removed)
        for crd in crd_names:
            with contextlib.suppress(Exception):
                self.kubectl.exec_command(f"kubectl delete crd {crd} --timeout=30s 2>/dev/null || true")

        logger.info("Force-removed all Chaos Mesh CRs and CRDs.")

    # ── Chaos Mesh installation ───────────────────────────────────────

    def _ensure_chaos_mesh_installed(self):
        """Check if Chaos Mesh is installed; install if missing."""
        try:
            ns_check = self.kubectl.exec_command(f"kubectl get ns {CHAOS_NAMESPACE}")
            if "Active" in ns_check:
                pods = self.kubectl.exec_command(
                    f"kubectl get pods -n {CHAOS_NAMESPACE} -l app.kubernetes.io/component=controller-manager"
                )
                if "Running" in pods:
                    self._chaos_mesh_ready = True
                    logger.info("Chaos Mesh is already installed and running.")
                    return

            logger.info("Chaos Mesh not found. Installing...")
            self.kubectl.exec_command("helm repo add chaos-mesh https://charts.chaos-mesh.org")
            self.kubectl.exec_command("helm repo update")
            self.kubectl.exec_command(f"kubectl create ns {CHAOS_NAMESPACE}")

            # Clean up orphaned CRDs if needed (strip finalizers first to avoid hanging)
            helm_check = self.kubectl.exec_command(f"helm list -n {CHAOS_NAMESPACE}")
            crd_check = self.kubectl.exec_command("kubectl get crd 2>/dev/null | grep chaos-mesh.org || true")
            if "chaos-mesh" not in helm_check and "chaos-mesh.org" in crd_check:
                logger.info("Cleaning up orphaned Chaos Mesh CRDs...")
                self._force_remove_all_chaos_resources()

            # Detect container runtime
            runtime, socket_path = "docker", "/var/run/docker.sock"
            try:
                nodes_info = self.kubectl.exec_command("kubectl get nodes -o wide")
                if "containerd" in nodes_info:
                    runtime = "containerd"
                    socket_path = "/run/containerd/containerd.sock"
                elif "crio" in nodes_info:
                    runtime = "crio"
                    socket_path = "/var/run/crio/crio.sock"
            except Exception:
                pass

            install_cmd = (
                f"helm upgrade --install chaos-mesh chaos-mesh/chaos-mesh "
                f"-n {CHAOS_NAMESPACE} --create-namespace --version 2.8.0 "
                f"--set chaosDaemon.runtime={runtime} "
                f"--set chaosDaemon.socketPath={socket_path}"
            )
            result = self.kubectl.exec_command(install_cmd)
            if "Error" in result and "has no deployed releases" not in result:
                logger.error(f"Failed to install Chaos Mesh: {result}")
                return

            # Wait for readiness
            for _ in range(30):
                pods_status = self.kubectl.exec_command(f"kubectl get pods -n {CHAOS_NAMESPACE}")
                if "Running" in pods_status and "0/1" not in pods_status and "ContainerCreating" not in pods_status:
                    self._chaos_mesh_ready = True
                    logger.info("Chaos Mesh installed successfully.")
                    return
                time.sleep(2)

            logger.warning("Chaos Mesh installation timed out.")
        except Exception as e:
            logger.error(f"Error ensuring Chaos Mesh installation: {e}")


def get_noise_manager() -> NoiseManager:
    """Global accessor for the singleton NoiseManager."""
    return NoiseManager()

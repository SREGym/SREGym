import logging
import time
import yaml
import tempfile
import os
import random
from sregym.generators.noise.base import BaseNoise
from sregym.generators.noise.impl import register_noise
from sregym.service.kubectl import KubeCtl

logger = logging.getLogger(__name__)

@register_noise("chaos_mesh")
class ChaosMeshNoise(BaseNoise):
    def __init__(self, config):
        super().__init__(config)
        self.kubectl = KubeCtl()
        self.base_experiment_name = config.get("experiment_name", "noise-experiment")
        self.active_experiments = []
        self.experiment_spec = config.get("spec", {})
        self.trigger_config = config.get("trigger", {})
        self.last_injection_time = 0
        self.duration = config.get("duration", 180)
        self.cooldown = config.get("cooldown", 300)
        self.namespace = config.get("namespace", "chaos-mesh")
        self.context = {}
        
        # Ensure Chaos Mesh is installed
        self._ensure_chaos_mesh_installed()
        
        # Default templates if no spec is provided
        self.templates = [
            {
                "kind": "PodChaos",
                "spec": {
                    "action": "pod-failure",
                    "mode": "one",
                    "selector": {"namespaces": ["{target_namespace}"]},
                    "duration": "{duration}"
                }
            },
            {
                "kind": "NetworkChaos",
                "spec": {
                    "action": "delay",
                    "mode": "one",
                    "selector": {"namespaces": ["{target_namespace}"]},
                    "delay": {"latency": "200ms", "correlation": "100", "jitter": "0ms"},
                    "duration": "{duration}"
                }
            }
        ]

    def _ensure_chaos_mesh_installed(self):
        """Check if Chaos Mesh is installed, and install it if not."""
        try:
            # Check if namespace exists
            ns_check = self.kubectl.exec_command(f"kubectl get ns {self.namespace}")
            if "Active" in ns_check:
                # Check if controller manager is running
                pods_check = self.kubectl.exec_command(f"kubectl get pods -n {self.namespace} -l app.kubernetes.io/component=controller-manager")
                if "Running" in pods_check:
                    logger.info("Chaos Mesh is already installed and running.")
                    return

            logger.info("Chaos Mesh not found. Attempting to install...")
            print("⚠️ Chaos Mesh not found. Installing automatically...")

            # Add helm repo
            self.kubectl.exec_command("helm repo add chaos-mesh https://charts.chaos-mesh.org")
            self.kubectl.exec_command("helm repo update")

            # Create namespace if not exists
            self.kubectl.exec_command(f"kubectl create ns {self.namespace}")

            # Install Chaos Mesh
            # Detect container runtime
            runtime = "docker"
            socket_path = "/var/run/docker.sock"
            
            # Try to detect runtime from nodes
            try:
                nodes_info = self.kubectl.exec_command("kubectl get nodes -o wide")
                if "containerd" in nodes_info:
                    runtime = "containerd"
                    socket_path = "/run/containerd/containerd.sock"
                elif "crio" in nodes_info:
                    runtime = "crio"
                    socket_path = "/var/run/crio/crio.sock"
            except:
                pass

            install_cmd = (
                f"helm install chaos-mesh chaos-mesh/chaos-mesh "
                f"-n {self.namespace} --version 2.8.0 "
                f"--set chaosDaemon.runtime={runtime} "
                f"--set chaosDaemon.socketPath={socket_path}"
            )
            
            logger.info(f"Installing Chaos Mesh with command: {install_cmd}")
            result = self.kubectl.exec_command(install_cmd)
            
            if "Error" in result and "already exists" not in result:
                logger.error(f"Failed to install Chaos Mesh: {result}")
                return

            # Wait for pods to be ready
            logger.info("Waiting for Chaos Mesh pods to be ready...")
            print("⏳ Waiting for Chaos Mesh to be ready...")
            # Simple wait loop
            for _ in range(30):
                pods_status = self.kubectl.exec_command(f"kubectl get pods -n {self.namespace}")
                if "Running" in pods_status and "0/1" not in pods_status and "ContainerCreating" not in pods_status:
                    logger.info("Chaos Mesh installed successfully.")
                    print("✅ Chaos Mesh installed successfully.")
                    return
                time.sleep(2)
            
            logger.warning("Chaos Mesh installation timed out waiting for pods.")

        except Exception as e:
            logger.error(f"Error ensuring Chaos Mesh installation: {e}")

    def inject(self, context=None):
        trigger_type = context.get("trigger") if context else "background"
        
        # Default to background if not specified
        configured_trigger = self.trigger_config.get("type", "background")
        
        # Enforce mutual exclusivity: trigger type must be a single string
        if isinstance(configured_trigger, list):
            logger.warning(f"ChaosMesh trigger type configuration error: {configured_trigger}. "
                         "Must be a single string ('background' or 'tool_call'). "
                         "Dual modes are not supported. Defaulting to 'background'.")
            configured_trigger = "background"
        
        if trigger_type != configured_trigger:
            return

        if trigger_type == "tool_call":
            tool_name = context.get("tool_name")
            command = context.get("command")
            target_tool = self.trigger_config.get("tool", "kubectl")
            target_cmd_pattern = self.trigger_config.get("command_pattern", "")
            
            if tool_name != target_tool:
                return
            if target_cmd_pattern and target_cmd_pattern not in command:
                return
        
        # Wait for context to be ready (namespace is required)
        if not self.context.get("namespace"):
            return

        now = time.time()
        if now - self.last_injection_time < self.cooldown:
            return

        # Select chaos configuration
        if self.experiment_spec:
            kind = self.experiment_spec.get("kind", "PodChaos")
            spec = self.experiment_spec.get("spec", {})
            action = spec.get("action", "custom")
        else:
            template = random.choice(self.templates)
            kind = template["kind"]
            spec = template["spec"]
            action = spec.get("action", "unknown")

        # Generate unique name with action type
        timestamp = int(time.time())
        safe_action = str(action).replace("_", "-").replace(" ", "-").lower()
        target_app = self.context.get("app_name", "generic").replace("_", "-").replace(" ", "-").lower()
        current_name = f"{self.base_experiment_name}-{target_app}-{safe_action}-{timestamp}-{random.randint(100, 999)}"

        logger.info(f"Injecting ChaosMesh noise: {current_name}")
        print(f"Injecting ChaosMesh noise: {current_name}")
        self._apply_chaos(current_name, kind, spec)
        self.last_injection_time = now

    def _apply_chaos(self, name, kind, spec):
        # Fill in dynamic values from context
        target_namespace = self.context.get("namespace", "default")
        print(f"Target namespace for chaos: {target_namespace}")
        # Deep copy and format strings in spec
        import copy
        spec = copy.deepcopy(spec)
        
        def format_dict(d):
            for k, v in d.items():
                if isinstance(v, dict):
                    format_dict(v)
                elif isinstance(v, list):
                    for i in range(len(v)):
                        if isinstance(v[i], str):
                            v[i] = v[i].format(target_namespace=target_namespace, duration=f"{self.duration}s")
                elif isinstance(v, str):
                    d[k] = v.format(target_namespace=target_namespace, duration=f"{self.duration}s")
        
        format_dict(spec)

        metadata = {
            "name": name,
            "namespace": self.namespace
        }
        
        crd = {
            "apiVersion": "chaos-mesh.org/v1alpha1",
            "kind": kind,
            "metadata": metadata,
            "spec": spec
        }
        
        try:
            with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as tmp:
                yaml.dump(crd, tmp)
                tmp_path = tmp.name
            
            cmd = f"kubectl apply -f {tmp_path}"
            out=self.kubectl.exec_command(cmd)
            logger.info(f"Applied chaos experiment {name}: {out}")
            os.remove(tmp_path)
            
            # Store for cleanup
            self.active_experiments.append({
                "name": name,
                "kind": kind
            })
            
        except Exception as e:
            logger.error(f"Failed to apply chaos experiment: {e}")

    def clean(self):
        logger.info(f"Cleaning up {len(self.active_experiments)} ChaosMesh experiments")
        for exp in self.active_experiments:
            try:
                cmd = f"kubectl delete {exp['kind']} {exp['name']} -n {self.namespace}"
                self.kubectl.exec_command(cmd)
            except Exception as e:
                logger.error(f"Failed to clean up chaos experiment {exp['name']}: {e}")
        self.active_experiments = []


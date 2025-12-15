from sregym.generators.noise.base import BaseNoise
from sregym.generators.noise.impl import register_noise
from sregym.service.kubectl import KubeCtl
import logging
import time
import random
import threading

logger = logging.getLogger(__name__)

@register_noise("node_maintenance")
class NodeMaintenanceNoise(BaseNoise):
    def __init__(self, config):
        super().__init__(config)
        self.kubectl = KubeCtl()
        self.interval = config.get("interval", 240) # Maintenance every 4 mins
        self.duration = config.get("duration", 120) # Maintenance lasts 120s
        self.last_maintenance_time = 0
        self.maintenance_lock = threading.Lock()
        self.active_maintenance = None # Stores (node_name, start_time)
        self.context = {}

    def inject(self, context=None):
        trigger = context.get("trigger", "background")
        if trigger != "background":
            return

        with self.maintenance_lock:
            now = time.time()
            
            # Check if we need to recover from maintenance
            if self.active_maintenance:
                node_name, start_time = self.active_maintenance
                if now - start_time >= self.duration:
                    self._recover_node(node_name)
                    self.active_maintenance = None
                    self.last_maintenance_time = now
                return

            # Check if we should start new maintenance
            if now - self.last_maintenance_time < self.interval:
                return

            # Start maintenance
            self._start_maintenance()

    def _start_maintenance(self):
        try:
            # Get list of nodes
            nodes_json = self.kubectl.exec_command("kubectl get nodes -o jsonpath='{.items[*].metadata.name}'")
            nodes = nodes_json.split()

            if len(nodes) < 2:
                logger.warning("Skipping node maintenance: Cluster has less than 2 nodes. Draining the only node would cause total outage.")
                return
            
            # Filter out control-plane if possible (simple heuristic)
            worker_nodes = [n for n in nodes if "control-plane" not in n and "master" not in n]
            if not worker_nodes:
                worker_nodes = nodes # Fallback to any node if no workers found (e.g. minikube)
            
            target_node = random.choice(worker_nodes)
            
            logger.info(f"Starting maintenance on node {target_node}")
            print(f"🔧 Starting maintenance on node {target_node}")
            
            # 1. Cordon
            self.kubectl.exec_command(f"kubectl cordon {target_node}")
            
            # 2. Drain (async or with timeout)
            # We run drain in a separate thread because it blocks
            threading.Thread(target=self._drain_node, args=(target_node,), daemon=True).start()
            
            self.active_maintenance = (target_node, time.time())
            
        except Exception as e:
            logger.error(f"Failed to start node maintenance: {e}")

    def _drain_node(self, node_name):
        try:
            logger.info(f"Draining node {node_name}")
            # Use --ignore-daemonsets and --delete-emptydir-data to ensure it works
            self.kubectl.exec_command(f"kubectl drain {node_name} --ignore-daemonsets --delete-emptydir-data --force --timeout=60s")
        except Exception as e:
            logger.error(f"Failed to drain node {node_name}: {e}")

    def _recover_node(self, node_name):
        try:
            logger.info(f"Recovering node {node_name}")
            print(f"✅ Recovering node {node_name}")
            self.kubectl.exec_command(f"kubectl uncordon {node_name}")
        except Exception as e:
            logger.error(f"Failed to recover node {node_name}: {e}")

    def clean(self):
        with self.maintenance_lock:
            if self.active_maintenance:
                node_name, _ = self.active_maintenance
                self._recover_node(node_name)
                self.active_maintenance = None
            
            # Ensure all nodes are uncordoned just in case
            try:
                nodes_json = self.kubectl.exec_command("kubectl get nodes -o jsonpath='{.items[*].metadata.name}'")
                for node in nodes_json.split():
                    self.kubectl.exec_command(f"kubectl uncordon {node}")
            except:
                pass

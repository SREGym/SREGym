"""TrainTicket Locust Workload Manager

Extends the base LocustWorkloadManager to provide TrainTicket-specific
workload generation capabilities.
"""

import logging
from typing import Optional, Dict, Any

from sregym.generators.workload.locust import LocustWorkloadManager
from sregym.service.kubectl import KubeCtl

logger = logging.getLogger(__name__)


class TrainTicketLocustWorkloadManager(LocustWorkloadManager):
    """TrainTicket-specific Locust workload manager.
    
    Manages Locust load generation for TrainTicket application,
    including specific scenarios for fault injection testing.
    """
    
    def __init__(self, namespace: str = "train-ticket", kubectl: Optional[KubeCtl] = None):
        """Initialize TrainTicket Locust workload manager.
        
        Args:
            namespace: Kubernetes namespace
            kubectl: Optional kubectl instance
        """
        super().__init__(namespace=namespace, kubectl=kubectl)
        self.locust_master_host = "locust-master"
        self.locust_web_port = 8089
        
    def start(self):
        """Start TrainTicket workload generation."""
        try:
            # First check if Locust is deployed
            if not self._is_locust_ready():
                logger.error("Locust deployment not ready")
                return
                
            # Start the fetcher pod
            super().start()
            
            print("[TrainTicket Locust] Workload manager started")
            print(f"[TrainTicket Locust] Access UI at http://<node-ip>:30089 (admin/admin)")
            
        except Exception as e:
            logger.error(f"Error starting TrainTicket workload: {e}")
            
    def trigger_f1_scenario(self, user_count: int = 10, spawn_rate: int = 2):
        """Trigger F1 fault scenario with order creation and cancellation.
        
        Args:
            user_count: Number of simulated users
            spawn_rate: Users spawned per second
        """
        try:
            print(f"[TrainTicket Locust] Triggering F1 scenario with {user_count} users")
            
            # Start the swarm with specific parameters
            result = self.kubectl.exec_command(
                f"kubectl exec deployment/locust-master -n {self.namespace} -- "
                f"curl -X POST http://localhost:{self.locust_web_port}/swarm "
                f"-d 'user_count={user_count}&spawn_rate={spawn_rate}'"
            )
            
            if result:
                print("[TrainTicket Locust] F1 scenario started - users will create and cancel orders")
                print("[TrainTicket Locust] This will trigger the 8-second delay fault if enabled")
            else:
                print("[TrainTicket Locust] Failed to start F1 scenario")
                
        except Exception as e:
            logger.error(f"Error triggering F1 scenario: {e}")
            
    def stop_workload(self):
        """Stop the current workload."""
        try:
            result = self.kubectl.exec_command(
                f"kubectl exec deployment/locust-master -n {self.namespace} -- "
                f"curl -X GET http://localhost:{self.locust_web_port}/stop"
            )
            
            if result:
                print("[TrainTicket Locust] Workload stopped")
            else:
                print("[TrainTicket Locust] Failed to stop workload")
                
        except Exception as e:
            logger.error(f"Error stopping workload: {e}")
            
    def get_stats(self) -> Dict[str, Any]:
        """Get current Locust statistics.
        
        Returns:
            Dict containing current workload statistics
        """
        try:
            result = self.kubectl.exec_command(
                f"kubectl exec deployment/locust-master -n {self.namespace} -- "
                f"curl -s http://localhost:{self.locust_web_port}/stats/requests"
            )
            
            if result:
                import json
                return json.loads(result)
            else:
                return {}
                
        except Exception as e:
            logger.error(f"Error getting stats: {e}")
            return {}
            
    def _is_locust_ready(self) -> bool:
        """Check if Locust deployment is ready.
        
        Returns:
            bool: True if Locust is deployed and ready
        """
        try:
            # Check if Locust master is running
            result = self.kubectl.exec_command(
                f"kubectl get deployment locust-master -n {self.namespace} "
                f"-o jsonpath='{{.status.readyReplicas}}'"
            )
            
            return result == "1"
            
        except Exception as e:
            logger.error(f"Error checking Locust readiness: {e}")
            return False
            
    def set_target_host(self, host: str):
        """Update the target host for load generation.
        
        Args:
            host: New target host URL
        """
        try:
            # Update the Locust target
            result = self.kubectl.exec_command(
                f"kubectl exec deployment/locust-master -n {self.namespace} -- "
                f"curl -X POST http://localhost:{self.locust_web_port}/swarm "
                f"-d 'host={host}'"
            )
            
            if result:
                print(f"[TrainTicket Locust] Target host updated to: {host}")
                
        except Exception as e:
            logger.error(f"Error updating target host: {e}")

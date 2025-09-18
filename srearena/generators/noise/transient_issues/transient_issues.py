import random
import time
import threading
from typing import List, Dict, Optional, Set
from dataclasses import dataclass
from enum import Enum
from kubernetes import client
from srearena.generators.noise.transient_issues.chaos_injector import ChaosInjector
from srearena.service.kubectl import KubeCtl


class FaultType(Enum):
    """Fault type enumeration"""
    FAIL_STOP = "fail-stop"  # Stop-type faults
    FAIL_SLOW = "fail-slow"  # Slow-down type faults


class PodScope(Enum):
    """Pod scope level enumeration"""
    TARGET_SERVICE = "target_service"           # Within target service
    TARGET_NAMESPACE = "target_namespace"       # Within target namespace
    NON_TARGET_SERVICE = "non_target_service"   # Outside target service but within target namespace
    ALL_PODS = "all_pods"                       # All pods
    NON_TARGET_NAMESPACE = "non_target_namespace"  # All pods outside target namespace


@dataclass
class TransientExperiment:
    """Represents a transient chaos experiment"""
    name: str
    experiment_type: str
    fault_type: FaultType
    target_pods: List[str]
    scope: PodScope
    duration: int  # Duration in seconds
    start_time: float
    cleanup_timer: Optional[threading.Timer] = None


class TransientIssuesGenerator:
    """Generate transient cluster issues as noise"""
    
    def __init__(self, namespace: str, target_services: List[str] = None, 
                 min_duration: int = 30, max_duration: int = 300):
        """
        Initialize transient issues generator
        
        Args:
            namespace: Target namespace
            target_services: List of target services, used to determine scope
            min_duration: Minimum duration in seconds
            max_duration: Maximum duration in seconds
        """
        self.namespace = namespace
        self.target_services = target_services or []
        self.min_duration = min_duration
        self.max_duration = max_duration
        self.kubectl = KubeCtl()
        self.chaos_injector = ChaosInjector(namespace)
        
        # Active transient experiments
        self.active_experiments: Dict[str, TransientExperiment] = {}

        # Continuous injection control
        self._injection_running = False
        self._injection_thread = None
        self._stop_event = threading.Event()
        
        # Available chaos experiment types and their configurations
        self.experiment_types = {
            # Fail-Stop type faults
            "pod-kill": {
                "weight": 3,
                "fault_type": FaultType.FAIL_STOP,
                "method": self._inject_pod_kill,
                "cleanup_method": self._cleanup_pod_kill,
                "description": "Randomly kill pods",
                "scopes": [PodScope.TARGET_SERVICE, PodScope.TARGET_NAMESPACE, 
                          PodScope.NON_TARGET_SERVICE, PodScope.ALL_PODS, PodScope.NON_TARGET_NAMESPACE]
            },
            "container-kill": {
                "weight": 2,
                "fault_type": FaultType.FAIL_STOP,
                "method": self._inject_container_kill,
                "cleanup_method": self._cleanup_container_kill,
                "description": "Kill containers",
                "scopes": [PodScope.TARGET_SERVICE, PodScope.TARGET_NAMESPACE, 
                          PodScope.NON_TARGET_SERVICE, PodScope.ALL_PODS, PodScope.NON_TARGET_NAMESPACE]
            },
            "pod-failure": {
                "weight": 2,
                "fault_type": FaultType.FAIL_STOP,
                "method": self._inject_pod_failure,
                "cleanup_method": self._cleanup_pod_failure,
                "description": "Inject pod failure",
                "scopes": [PodScope.TARGET_SERVICE, PodScope.TARGET_NAMESPACE, 
                          PodScope.NON_TARGET_SERVICE, PodScope.ALL_PODS, PodScope.NON_TARGET_NAMESPACE]
            },
            "network-loss": {
                "weight": 1,
                "fault_type": FaultType.FAIL_STOP,
                "method": self._inject_network_loss,
                "cleanup_method": self._cleanup_network_loss,
                "description": "Inject network packet loss",
                "scopes": [PodScope.TARGET_SERVICE, PodScope.TARGET_NAMESPACE, 
                          PodScope.NON_TARGET_SERVICE, PodScope.ALL_PODS, PodScope.NON_TARGET_NAMESPACE]
            },
            "network-partition": {
                "weight": 1,
                "fault_type": FaultType.FAIL_STOP,
                "method": self._inject_network_partition,
                "cleanup_method": self._cleanup_network_partition,
                "description": "Inject network partition between services",
                "scopes": [PodScope.TARGET_SERVICE, PodScope.TARGET_NAMESPACE, 
                          PodScope.NON_TARGET_SERVICE, PodScope.ALL_PODS, PodScope.NON_TARGET_NAMESPACE]
            },
            
            # Fail-Slow type faults
            "network-delay": {
                "weight": 2,
                "fault_type": FaultType.FAIL_SLOW,
                "method": self._inject_network_delay,
                "cleanup_method": self._cleanup_network_delay,
                "description": "Inject network delay",
                "scopes": [PodScope.TARGET_SERVICE, PodScope.TARGET_NAMESPACE, 
                          PodScope.NON_TARGET_SERVICE, PodScope.ALL_PODS, PodScope.NON_TARGET_NAMESPACE]
            },
            "cpu-stress": {
                "weight": 2,
                "fault_type": FaultType.FAIL_SLOW,
                "method": self._inject_cpu_stress,
                "cleanup_method": self._cleanup_cpu_stress,
                "description": "Inject CPU stress",
                "scopes": [PodScope.TARGET_SERVICE, PodScope.TARGET_NAMESPACE, 
                          PodScope.NON_TARGET_SERVICE, PodScope.ALL_PODS, PodScope.NON_TARGET_NAMESPACE]
            },
            "memory-stress": {
                "weight": 1,
                "fault_type": FaultType.FAIL_SLOW,
                "method": self._inject_memory_stress,
                "cleanup_method": self._cleanup_memory_stress,
                "description": "Inject memory stress",
                "scopes": [PodScope.TARGET_SERVICE, PodScope.TARGET_NAMESPACE, 
                          PodScope.NON_TARGET_SERVICE, PodScope.ALL_PODS, PodScope.NON_TARGET_NAMESPACE]
            }
        }


    ### Injection and cleanup
    def inject_transient_issue(self, fault_types: List[FaultType] = None, 
                             scopes: List[PodScope] = None, experiment: str = None) -> Optional[str]:
        """Inject a transient issue"""
        try:
            # Randomly select experiment type
            experiment_type = self.select_random_experiment_type(fault_types, scopes)
            if experiment:
                experiment_type = experiment
            if not experiment_type:
                return None
            
            # Randomly select scope
            if scopes:
                available_scopes = [s for s in scopes if s in self.experiment_types[experiment_type]["scopes"]]
            else:
                available_scopes = self.experiment_types[experiment_type]["scopes"]
            
            if not available_scopes:
                print("No available scopes")
                return None
            
            selected_scope = random.choice(available_scopes)
            # Get target pods based on scope
            count = 2 if experiment_type in ["network-partition"] else 1
            target_pods = self.get_pods_by_scope(selected_scope, count)
            if not target_pods:
                print("No available target pods, skipping injection")
                return None
            
            # Randomly generate duration
            duration = random.randint(self.min_duration, self.max_duration)
            
            # Generate experiment name
            experiment_name = self.generate_experiment_name(experiment_type)
            
            fault_type = self.experiment_types[experiment_type]["fault_type"]
            
            print(f"🔥 Injecting transient issue: {experiment_type}")
            print(f"   Experiment name: {experiment_name}")
            print(f"   Fault type: {fault_type.value}")
            print(f"   Scope level: {selected_scope.value}")
            print(f"   Target services: {target_pods}")
            print(f"   Duration: {duration} seconds")
            print(f"   Description: {self.experiment_types[experiment_type]['description']}")
            
            # Create experiment record
            experiment = TransientExperiment(
                name=experiment_name,
                experiment_type=experiment_type,
                fault_type=fault_type,
                target_pods=target_pods,
                scope=selected_scope,
                duration=duration,
                start_time=time.time()
            )
            
            # Execute injection
            inject_method = self.experiment_types[experiment_type]["method"]
            success = inject_method(experiment_name, target_pods)
            
            if success:
                # Set auto cleanup timer
                cleanup_timer = threading.Timer(
                    duration,
                    self._auto_cleanup,
                    args=[experiment_name]
                )
                experiment.cleanup_timer = cleanup_timer
                cleanup_timer.start()
                
                # Record active experiment
                self.active_experiments[experiment_name] = experiment
                
                print(f"✅ Transient issue injection successful, will auto recover in {duration} seconds")
                return experiment_name
            else:
                print(f"❌ Transient issue injection failed")
                return None
                
        except Exception as e:
            raise RuntimeError(f"Error injecting transient issue: {e}") from e

    def _auto_cleanup(self, experiment_name: str):
        """Automatically cleanup experiment"""
        try:
            if experiment_name in self.active_experiments:
                experiment = self.active_experiments[experiment_name]
                elapsed = time.time() - experiment.start_time
                
                print(f"🔄 Auto recovering transient issue: {experiment.experiment_type}")
                print(f"   Experiment name: {experiment_name}")
                print(f"   Fault type: {experiment.fault_type.value}")
                print(f"   Scope level: {experiment.scope.value}")
                print(f"   Actual duration: {elapsed:.1f} seconds")
                
                # Execute cleanup
                cleanup_method = self.experiment_types[experiment.experiment_type]["cleanup_method"]
                cleanup_method(experiment_name)
                # Remove record
                del self.active_experiments[experiment_name]
                
                print(f"✅ Transient issue auto recovered")
                
        except Exception as e:
            raise RuntimeError(f"Error auto cleaning experiment: {e}") from e

    def cleanup_experiment(self, experiment_name: str) -> bool:
        """Manually cleanup specified experiment"""
        try:
            if experiment_name not in self.active_experiments:
                print(f"Experiment {experiment_name} does not exist or has been cleaned up")
                return False
            
            experiment = self.active_experiments[experiment_name]
            
            # Cancel timer
            if experiment.cleanup_timer:
                experiment.cleanup_timer.cancel()
            
            # Execute cleanup
            cleanup_method = self.experiment_types[experiment.experiment_type]["cleanup_method"]
            cleanup_method(experiment_name)
            
            # Remove record
            del self.active_experiments[experiment_name]
            
            print(f"✅ Manual cleanup of experiment {experiment_name} successful")
            return True
            
        except Exception as e:
            raise RuntimeError(f"Error manually cleaning experiment: {e}") from e

    def cleanup_all_experiments(self):
        """Cleanup all active experiments"""
        experiment_names = list(self.active_experiments.keys())
        for name in experiment_names:
            self.cleanup_experiment(name)
        experiment_types=["PodChaos", "NetworkChaos", "StressChaos"]
        for experiment_type in experiment_types:
            delete_cmd = f"kubectl delete {experiment_type} --all -n chaos-mesh --ignore-not-found=true"
            self.kubectl.exec_command(delete_cmd)


    def start_continuous_injection(self, fault_types: List[FaultType] = None,
                                 scopes: List[PodScope] = None,
                                 interval_min: int = 60, interval_max: int = 300):
        """Start continuous transient issue injection
        
        Args:
            fault_types: List of allowed fault types, None means all types
            scopes: List of allowed scope levels, None means all scopes
            interval_min: Minimum injection interval in seconds
            interval_max: Maximum injection interval in seconds
        """
        if self._injection_running:
            print("⚠️ Continuous injection already running, please stop current injection first")
            return False
        
        def injection_loop():
            print(f"🚀 Continuous transient issue injection started")
            print(f"   Allowed fault types: {[ft.value for ft in fault_types] if fault_types else 'All types'}")
            print(f"   Allowed scope levels: {[s.value for s in scopes] if scopes else 'All scopes'}")
            print(f"   Injection interval: {interval_min}-{interval_max} seconds")
            
            while not self._stop_event.is_set():
                try:
                    # Inject a transient issue
                    self.inject_transient_issue(fault_types, scopes)
                    
                    # Wait randomly for next injection, but check stop signal periodically
                    next_interval = random.randint(interval_min, interval_max)
                    print(f"⏰ Next injection will be in {next_interval} seconds")
                    
                    # Wait in segments, checking stop signal every second
                    for _ in range(next_interval):
                        if self._stop_event.is_set():
                            break
                        time.sleep(1)
                    
                except Exception as e:
                    # For continuous injection loops, still use print to log errors but continue running
                    print(f"Error in continuous injection loop: {e}")
                    self.cleanup_all_experiments()
            
            print("🛑 Continuous transient issue injection stopped")
            self._injection_running = False
        
        # Reset stop event
        self._stop_event.clear()
        
        # Run in background thread
        self._injection_thread = threading.Thread(target=injection_loop, daemon=True)
        self._injection_running = True
        self._injection_thread.start()
        
        return True

    def stop_continuous_injection(self, cleanup_active: bool = True) -> bool:
        """Stop continuous transient issue injection
        
        Args:
            cleanup_active: Whether to cleanup currently active experiments, default True
            
        Returns:
            bool: Whether successfully stopped
        """
        if not self._injection_running:
            print("⚠️ Continuous injection not running")
            return False
        
        print("🛑 Stopping continuous transient issue injection...")
        
        # Set stop signal
        self._stop_event.set()
        
        # Wait for injection thread to end
        if self._injection_thread and self._injection_thread.is_alive():
            self._injection_thread.join(timeout=10)  # Wait at most 10 seconds
            
            if self._injection_thread.is_alive():
                print("⚠️ Injection thread failed to stop within 10 seconds")
                return False
        
        # Optional: cleanup currently active experiments
        if cleanup_active and self.active_experiments:
            print(f"🧹 Cleaning up {len(self.active_experiments)} active experiments...")
            self.cleanup_all_experiments()
        
        self._injection_running = False
        print("✅ Continuous transient issue injection successfully stopped")
        return True

    def restart_continuous_injection(self, fault_types: List[FaultType] = None,
                                   scopes: List[PodScope] = None,
                                   interval_min: int = 60, interval_max: int = 300,
                                   cleanup_active: bool = False) -> bool:
        """Restart continuous injection
        
        Args:
            fault_types: List of allowed fault types
            scopes: List of allowed scope levels
            interval_min: Minimum injection interval in seconds
            interval_max: Maximum injection interval in seconds
            cleanup_active: Whether to cleanup active experiments before restart
            
        Returns:
            bool: Whether successfully restarted
        """
        print("🔄 Restarting continuous transient issue injection...")
        
        # First stop current injection
        if self._injection_running:
            if not self.stop_continuous_injection(cleanup_active):
                return False
        
        # Start new injection
        return self.start_continuous_injection(fault_types, scopes, interval_min, interval_max)


    ### Status and statistics
    def get_active_experiments(self) -> List[Dict]:
        """Get information of currently active experiments"""
        result = []
        current_time = time.time()
        
        for name, experiment in self.active_experiments.items():
            elapsed = current_time - experiment.start_time
            remaining = max(0, experiment.duration - elapsed)
            
            result.append({
                "name": name,
                "type": experiment.experiment_type,
                "fault_type": experiment.fault_type.value,
                "scope": experiment.scope.value,
                "target_pods": experiment.target_pods,
                "duration": experiment.duration,
                "elapsed": round(elapsed, 1),
                "remaining": round(remaining, 1),
                "description": self.experiment_types[experiment.experiment_type]["description"]
            })
        
        return result

    def get_statistics(self) -> Dict:
        """Get injection statistics"""
        stats = {
            "total_active": len(self.active_experiments),
            "by_fault_type": {},
            "by_scope": {},
            "by_experiment_type": {}
        }
        
        for experiment in self.active_experiments.values():
            # Statistics by fault type
            fault_type = experiment.fault_type.value
            stats["by_fault_type"][fault_type] = stats["by_fault_type"].get(fault_type, 0) + 1
            
            # Statistics by scope
            scope = experiment.scope.value
            stats["by_scope"][scope] = stats["by_scope"].get(scope, 0) + 1
            
            # Statistics by experiment type
            exp_type = experiment.experiment_type
            stats["by_experiment_type"][exp_type] = stats["by_experiment_type"].get(exp_type, 0) + 1
        
        return stats


    ### Specific injection methods & cleanup implementations
    def _inject_pod_kill(self, experiment_name: str, target_pods: List[Dict[str,str]]) -> bool:
        """Inject pod kill fault"""
        try:
            # target_namespace = self.namespace if scope != PodScope.NON_TARGET_NAMESPACE else "default"
            
            # Get actual labels of target pods
            target_pod = random.choice(target_pods)
            target_namespace = target_pod['namespace']
            target_label = target_pod['service_label']
            label_selector = self._get_pod_label_selector(target_label, target_namespace)
            
            if not label_selector:
                print(f"Cannot find suitable label selector for pod {target_pod}")
                return False
            
            chaos_experiment = {
                "apiVersion": "chaos-mesh.org/v1alpha1",
                "kind": "PodChaos",
                "metadata": {"name": experiment_name, "namespace": 'chaos-mesh'},
                "spec": {
                    "action": "pod-kill",
                    "mode": "one",
                    "selector": {
                        "namespaces": [target_namespace],
                        "labelSelectors": label_selector
                    }
                }
            }
            self.chaos_injector.create_chaos_experiment(chaos_experiment, experiment_name)
            return True
        except Exception as e:
            raise RuntimeError(f"Failed to inject pod kill: {e}") from e

    def _cleanup_pod_kill(self, experiment_name: str):
        """Cleanup pod kill fault"""
        try:
            self.chaos_injector.delete_chaos_experiment(experiment_name)
        except Exception as e:
            raise RuntimeError(f"Failed to cleanup pod kill experiment {experiment_name}: {e}") from e

    def _inject_network_delay(self, experiment_name: str, target_pods: List[str]) -> bool:
        """Inject network delay fault"""
        try:
            latencies = ["100ms", "200ms", "500ms", "1s", "2s"]
            latency = random.choice(latencies)
            # target_namespace = self.namespace if scope != PodScope.NON_TARGET_NAMESPACE else "default"

            
            target_pod = random.choice(target_pods)
            target_namespace = target_pod['namespace']
            target_label = target_pod['service_label']
            label_selector = self._get_pod_label_selector(target_label, target_namespace)

            if not label_selector:
                print(f"Cannot find suitable label selector for pod {target_pod}")
                return False

            chaos_experiment = {
                "apiVersion": "chaos-mesh.org/v1alpha1",
                "kind": "NetworkChaos",
                "metadata": {"name": experiment_name, "namespace": 'chaos-mesh'},
                "spec": {
                    "action": "delay",
                    "mode": "one",
                    "selector": {
                        "namespaces": [target_namespace],
                        "labelSelectors": label_selector
                    },
                    "delay": {"latency": latency, "correlation": "100", "jitter": "0ms"}
                }
            }
            self.chaos_injector.create_chaos_experiment(chaos_experiment, experiment_name)
            return True
        except Exception as e:
            raise RuntimeError(f"Failed to inject network delay: {e}") from e

    def _cleanup_network_delay(self, experiment_name: str):
        """Cleanup network delay fault"""
        try:
            self.chaos_injector.delete_chaos_experiment(experiment_name)
        except Exception as e:
            raise RuntimeError(f"Failed to cleanup network delay experiment {experiment_name}: {e}") from e

    def _inject_cpu_stress(self, experiment_name: str, target_pods: List[str]) -> bool:
        """Inject CPU stress fault"""
        try:
            target_pod = random.choice(target_pods)
            target_namespace = target_pod['namespace']
            target_label = target_pod['service_label']

            label_selector = self._get_pod_label_selector(target_label, target_namespace)
            
            if not label_selector:
                print(f"Cannot find suitable label selector for pod {target_pod}")
                return False

            chaos_experiment = {
                "apiVersion": "chaos-mesh.org/v1alpha1",
                "kind": "StressChaos",
                "metadata": {"name": experiment_name, "namespace": 'chaos-mesh'},
                "spec": {
                    "mode": "one",
                    "selector": {
                        "namespaces": [target_namespace],
                        "labelSelectors": label_selector
                    },
                    "stressors": {
                        "cpu": {
                            "workers": random.randint(1, 4),
                            "load": random.randint(50, 100)
                        }
                    }
                }
            }
            self.chaos_injector.create_chaos_experiment(chaos_experiment, experiment_name)
            return True
        except Exception as e:
            raise RuntimeError(f"Failed to inject CPU stress: {e}") from e

    def _cleanup_cpu_stress(self, experiment_name: str):
        """Cleanup CPU stress fault"""
        try:
            self.chaos_injector.delete_chaos_experiment(experiment_name)
        except Exception as e:
            raise RuntimeError(f"Failed to cleanup CPU stress experiment {experiment_name}: {e}") from e

    def _inject_memory_stress(self, experiment_name: str, target_pods: List[str]) -> bool:
        """Inject memory stress fault"""
        try:
            target_pod = random.choice(target_pods)
            memory_sizes = ["50%", "70%", "80%"]
            memory_size = random.choice(memory_sizes)
            # target_namespace = self.namespace if scope != PodScope.NON_TARGET_NAMESPACE else "default"
            target_namespace = target_pod['namespace']
            target_label = target_pod['service_label']
            
            label_selector = self._get_pod_label_selector(target_label, target_namespace)

            if not label_selector:
                print(f"Cannot find suitable label selector for pod {target_pod}")
                return False

            chaos_experiment = {
                "apiVersion": "chaos-mesh.org/v1alpha1",
                "kind": "StressChaos",
                "metadata": {"name": experiment_name, "namespace": 'chaos-mesh'},
                "spec": {
                    "mode": "one",
                    "selector": {
                        "namespaces": [target_namespace],
                        "labelSelectors": label_selector
                    },
                    "stressors": {
                        "memory": {
                            "workers": random.randint(1, 4),
                            "size": memory_size
                        }
                    }
                }
            }
            self.chaos_injector.create_chaos_experiment(chaos_experiment, experiment_name)
            return True
        except Exception as e:
            raise RuntimeError(f"Failed to inject memory stress: {e}") from e

    def _cleanup_memory_stress(self, experiment_name: str):
        """Cleanup memory stress fault"""
        try:
            self.chaos_injector.delete_chaos_experiment(experiment_name)
        except Exception as e:
            raise RuntimeError(f"Failed to cleanup memory stress experiment {experiment_name}: {e}") from e

    def _inject_network_loss(self, experiment_name: str, target_pods: List[str]) -> bool:
        """Inject network packet loss fault"""
        try:
            loss_rates = ["10", "20", "30", "50"]
            loss_rate = random.choice(loss_rates)
            # target_namespace = self.namespace if scope != PodScope.NON_TARGET_NAMESPACE else "default"
            
            target_pod = random.choice(target_pods)
            target_namespace = target_pod['namespace']
            target_label = target_pod['service_label']
            label_selector = self._get_pod_label_selector(target_label, target_namespace)

            if not label_selector:
                print(f"Cannot find suitable label selector for pod {target_pod}")
                return False

            chaos_experiment = {
                "apiVersion": "chaos-mesh.org/v1alpha1",
                "kind": "NetworkChaos",
                "metadata": {"name": experiment_name, "namespace": 'chaos-mesh'},
                "spec": {
                    "action": "loss",
                    "mode": "one",
                    "selector": {
                        "namespaces": [target_namespace],
                        "labelSelectors": label_selector
                    },
                    "loss": {"loss": loss_rate, "correlation": "100"}
                }
            }
            self.chaos_injector.create_chaos_experiment(chaos_experiment, experiment_name)
            return True
        except Exception as e:
            raise RuntimeError(f"Failed to inject network loss: {e}") from e

    def _cleanup_network_loss(self, experiment_name: str):
        """Cleanup network packet loss fault"""
        try:
            self.chaos_injector.delete_chaos_experiment(experiment_name)
        except Exception as e:
            raise RuntimeError(f"Failed to cleanup network loss experiment {experiment_name}: {e}") from e

    def _inject_container_kill(self, experiment_name: str, target_pods: List[str]) -> bool:
        """Inject container kill fault"""
        try:
            # target_namespace = self.namespace if scope != PodScope.NON_TARGET_NAMESPACE else "default"
            
            target_pod = random.choice(target_pods)
            target_namespace = target_pod['namespace']
            target_label = target_pod['service_label']
            label_selector = self._get_pod_label_selector(target_label, target_namespace)

            if not label_selector:
                print(f"Cannot find suitable label selector for pod {target_pod}")
                return False

            # Get container names
            container_names = self._get_container_names(target_label, target_namespace)
            if not container_names:
                print(f"Cannot get container names for service {target_pod}, skipping container kill fault")
                return False
            
            # Randomly select containers to kill
            if len(container_names) == 1:
                # Only one container, select it directly
                selected_containers = container_names
            else:
                # Multiple containers, randomly decide how many to kill
                num_to_kill = random.randint(1, len(container_names))
                selected_containers = random.sample(container_names, num_to_kill)
            
            print(f"Will kill containers: {selected_containers}")

            chaos_experiment = {
                "apiVersion": "chaos-mesh.org/v1alpha1",
                "kind": "PodChaos",
                "metadata": {"name": experiment_name, "namespace": 'chaos-mesh'},
                "spec": {
                    "action": "container-kill",
                    "mode": "one",
                    "selector": {
                        "namespaces": [target_namespace],
                        "labelSelectors": label_selector
                    },
                    "containerNames": selected_containers
                }
            }
            self.chaos_injector.create_chaos_experiment(chaos_experiment, experiment_name)
            return True
        except Exception as e:
            raise RuntimeError(f"Failed to inject container kill: {e}") from e

    def _cleanup_container_kill(self, experiment_name: str):
        """Cleanup container kill fault"""
        try:
            self.chaos_injector.delete_chaos_experiment(experiment_name)
        except Exception as e:
            raise RuntimeError(f"Failed to cleanup container kill experiment {experiment_name}: {e}") from e

    def _inject_pod_failure(self, experiment_name: str, target_pods: List[str]) -> bool:
        """Inject pod failure fault"""
        try:
            target_pod = random.choice(target_pods)
            target_namespace = target_pod['namespace']
            target_label = target_pod['service_label']
            label_selector = self._get_pod_label_selector(target_label, target_namespace)

            if not label_selector:
                print(f"Cannot find suitable label selector for pod {target_pod}")
                return False

            chaos_experiment = {
                "apiVersion": "chaos-mesh.org/v1alpha1",
                "kind": "PodChaos",
                "metadata": {"name": experiment_name, "namespace": 'chaos-mesh'},
                "spec": {
                    "action": "pod-failure",
                    "mode": "one",
                    "selector": {
                        "namespaces": [target_namespace],
                        "labelSelectors": label_selector
                    }
                }
            }
            self.chaos_injector.create_chaos_experiment(chaos_experiment, experiment_name)
            return True
        except Exception as e:
            raise RuntimeError(f"Failed to inject pod failure: {e}") from e

    def _cleanup_pod_failure(self, experiment_name: str):
        """Cleanup pod failure fault"""
        try:
            self.chaos_injector.delete_chaos_experiment(experiment_name)
        except Exception as e:
            raise RuntimeError(f"Failed to cleanup pod failure experiment {experiment_name}: {e}") from e

    def _inject_network_partition(self, experiment_name: str, target_pods: List[str]) -> bool:
        """Inject network partition fault"""
        try:
            # For network partition, we need at least 2 different services
            if len(target_pods) < 2:
                print("Network partition requires target pods, skipping injection")
                return False

            from_pod = target_pods[0]
            to_pod = target_pods[1]

            from_namespace = from_pod['namespace']
            to_namespace = to_pod['namespace']
            from_service = from_pod['service_label']
            to_service = to_pod['service_label']
            
            # Get label selectors for both services
            from_label_selector = self._get_pod_label_selector(from_service, from_namespace)
            to_label_selector = self._get_pod_label_selector(to_service, to_namespace)
            
            if not from_label_selector or not to_label_selector:
                print(f"Cannot find suitable label selectors for partition: {from_service} -> {to_service}")
                return False
            
            chaos_experiment = {
                "apiVersion": "chaos-mesh.org/v1alpha1",
                "kind": "NetworkChaos",
                "metadata": {"name": experiment_name, "namespace": 'chaos-mesh'},
                "spec": {
                    "action": "partition",
                    "mode": "all",
                    "selector": {
                        "namespaces": [from_namespace],
                        "labelSelectors": from_label_selector
                    },
                    "direction": "to",
                    "target": {
                        "mode": "all",
                        "selector": {
                            "namespaces": [to_namespace],
                            "labelSelectors": to_label_selector
                        }
                    }
                }
            }
            
            print(f"Creating network partition: {from_service}@{from_namespace} -> {to_service}@{to_namespace}")
            self.chaos_injector.create_chaos_experiment(chaos_experiment, experiment_name)
            return True
            
        except Exception as e:
            raise RuntimeError(f"Failed to inject network partition: {e}") from e

    def _cleanup_network_partition(self, experiment_name: str):
        """Cleanup network partition fault"""
        try:
            self.chaos_injector.delete_chaos_experiment(experiment_name)
        except Exception as e:
            raise RuntimeError(f"Failed to cleanup network partition experiment {experiment_name}: {e}") from e


    ### help methods
    def get_pods_by_scope(self, scope: PodScope, count: int) -> List[Dict[str, str]]:
        """Get pod list based on scope level"""
        try:
            v1 = client.CoreV1Api()
            all_pods = []
            
            # Step 1: Determine target namespace list based on PodScope
            target_namespaces = self._get_target_namespaces(scope)
            if not target_namespaces:
                print(f"No suitable namespace found in scope {scope.value}")
                return []
            # Step 2: Collect qualifying pods in target namespace
            for target_namespace in target_namespaces:
                pods = v1.list_namespaced_pod(namespace=target_namespace)
                
                for pod in pods.items:
                    labels = pod.metadata.labels or {}
                    service_name = None
                    
                    # Extract service name
                    if "app.kubernetes.io/component" in labels:
                        service_name = labels["app.kubernetes.io/component"]
                    elif "app.kubernetes.io/name" in labels:
                        service_name = labels["app.kubernetes.io/name"]
                    elif "io.kompose.service" in labels:
                        service_name = labels["io.kompose.service"]
                    elif "openebs.io/component-name" in labels:
                        service_name = labels["openebs.io/component-name"]
                    elif "service" in labels:
                        service_name = labels["service"]
                    elif "app" in labels:
                        service_name = labels["app"]

                    if service_name:
                        # Decide whether to include this pod based on scope
                        if self._should_include_pod(service_name, target_namespace, scope):
                            all_pods.append({
                                'service_label': service_name, 
                                'namespace': target_namespace
                            })

            # Step 3: Deduplication
            unique_pods = self._deduplicate_pods(all_pods)
            
            if not unique_pods:
                print(f"No suitable pods found in scope {scope.value}")
                return []
            
            # Step 4: Random selection
            selected = random.sample(unique_pods, min(count, len(unique_pods)))
            return selected
            
        except Exception as e:
            raise RuntimeError(f"Error getting pod list for scope {scope.value}: {e}") from e

    def _get_target_namespaces(self, scope: PodScope) -> List[str]:
        """Get target namespace list based on PodScope"""
        try:
            v1 = client.CoreV1Api()
            target_namespaces = []
            if scope == PodScope.NON_TARGET_NAMESPACE:
                # Get all namespaces (excluding target namespace and system namespaces)
                namespaces = v1.list_namespace()
                for ns in namespaces.items:
                    ns_name = ns.metadata.name
                    if (ns_name != self.namespace and 
                        not ns_name.startswith('kube-') and 
                        not ns_name.startswith('chaos-') and
                        not ns_name.startswith('khaos') and
                        ns_name != 'default'):
                        target_namespaces.append(ns_name)
                # return target_namespaces
                
            elif scope == PodScope.ALL_PODS:
                # Get all namespaces in cluster (excluding system namespaces)
                namespaces = v1.list_namespace()
                for ns in namespaces.items:
                    ns_name = ns.metadata.name
                    if (not ns_name.startswith('kube-') and 
                        not ns_name.startswith('chaos-') and
                        not ns_name.startswith('khaos') and
                        ns_name != 'default'):
                        target_namespaces.append(ns_name)
                # return target_namespaces
                
            else:
                # TARGET_SERVICE, TARGET_NAMESPACE, NON_TARGET_SERVICE
                # These are all limited to target namespace
                target_namespaces.append(self.namespace)

            # select one namespace randomly
            if target_namespaces:
                return target_namespaces
            else:
                return []

        except Exception as e:
            raise RuntimeError(f"Error getting target namespaces: {e}") from e

    def _should_include_pod(self, service_name: str, namespace: str, scope: PodScope) -> bool:
        """Determine whether to include specified pod based on scope"""
        if scope == PodScope.TARGET_SERVICE:
            # Only include target services, and must be within target namespace
            return (namespace == self.namespace and 
                    service_name in self.target_services)
        
        elif scope == PodScope.NON_TARGET_SERVICE:
            # Exclude target services, but must be within target namespace
            return (namespace == self.namespace and 
                    service_name not in self.target_services)
        
        elif scope == PodScope.TARGET_NAMESPACE:
            # Include all services within target namespace
            return namespace == self.namespace
        
        elif scope in [PodScope.NON_TARGET_NAMESPACE, PodScope.ALL_PODS]:
            # For cross-namespace scenarios, include all valid services
            return True
        
        return False

    def _deduplicate_pods(self, all_pods: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """Deduplicate pods list"""
        unique_pods = []
        seen = set()
        
        for pod_info in all_pods:
            identifier = f"{pod_info['service_label']}:{pod_info['namespace']}"
            if identifier not in seen:
                seen.add(identifier)
                unique_pods.append(pod_info)
        
        return unique_pods

    def select_random_experiment_type(self, fault_types: List[FaultType] = None, 
                                    scopes: List[PodScope] = None) -> Optional[str]:
        """Randomly select experiment type based on weights and filter conditions"""
        # Filter qualifying experiment types
        available_types = []
        weights = []
        
        for exp_type, config in self.experiment_types.items():
            # Check fault type filter
            if fault_types and config["fault_type"] not in fault_types:
                continue
            
            # Check scope filter
            if scopes and not any(scope in config["scopes"] for scope in scopes):
                continue
            
            available_types.append(exp_type)
            weights.append(config["weight"])
        
        if not available_types:
            print("No qualifying experiment types")
            return None
        
        return random.choices(available_types, weights=weights, k=1)[0]

    def generate_experiment_name(self, experiment_type: str) -> str:
        """Generate unique experiment name"""
        timestamp = int(time.time())
        random_suffix = random.randint(1000, 9999)
        return f"transient-{experiment_type}-{timestamp}-{random_suffix}"
    
    def _get_pod_label_selector(self, target_service: str, namespace: str) -> Dict[str, str]:
        """Get correct label selector based on service name"""
        try:
            v1 = client.CoreV1Api()
            pods = v1.list_namespaced_pod(namespace=namespace)
            
            for pod in pods.items:
                labels = pod.metadata.labels or {}
                
                # Check different label formats
                if "app.kubernetes.io/component" in labels and labels["app.kubernetes.io/component"] == target_service:
                    return {"app.kubernetes.io/component": target_service}
                elif "io.kompose.service" in labels and labels["io.kompose.service"] == target_service:
                    return {"io.kompose.service": target_service}
                elif "openebs.io/component-name" in labels and labels["openebs.io/component-name"] == target_service:
                    return {"openebs.io/component-name": target_service}
                elif "app.kubernetes.io/name" in labels and labels["app.kubernetes.io/name"] == target_service:
                    return {"app.kubernetes.io/name": target_service}
                elif "app" in labels and labels["app"] == target_service:
                    return {"app": target_service}
                elif "service" in labels and labels["service"] == target_service:
                    return {"service": target_service}
            
            print(f"Label selector not found for service {target_service}")
            return {}
            
        except Exception as e:
            raise RuntimeError(f"Error getting label selector: {e}") from e

    def _get_container_names(self, target_service: str, namespace: str) -> List[str]:
        """Get container name list for specified service"""
        try:
            v1 = client.CoreV1Api()
            pods = v1.list_namespaced_pod(namespace=namespace)
            
            for pod in pods.items:
                labels = pod.metadata.labels or {}
                
                # Check if it's a pod of target service
                service_match = False
                if "app.kubernetes.io/component" in labels and labels["app.kubernetes.io/component"] == target_service:
                    service_match = True
                elif "io.kompose.service" in labels and labels["io.kompose.service"] == target_service:
                    service_match = True
                elif "app.kubernetes.io/name" in labels and labels["app.kubernetes.io/name"] == target_service:
                    service_match = True
                elif "service" in labels and labels["service"] == target_service:
                    service_match = True
                elif "openebs.io/component-name" in labels and labels["openebs.io/component-name"] == target_service:
                    service_match = True
                elif "app" in labels and labels["app"] == target_service:
                    service_match = True
                
                if service_match and pod.spec.containers:
                    # Return all container names of first matching pod
                    container_names = [container.name for container in pod.spec.containers]
                    print(f"Found containers for service {target_service}: {container_names}")
                    return container_names
            
            print(f"Containers not found for service {target_service}")
            return []
            
        except Exception as e:
            raise RuntimeError(f"Error getting container names: {e}") from e

# Usage example
if __name__ == "__main__":
    # Create generator
    generator = TransientIssuesGenerator(
        namespace="hotel-reservation",
        target_services=["frontend"],  # Specify target services
        min_duration=30,    # Minimum duration 30 seconds
        max_duration=50    # Maximum duration 50 seconds
    )
    
    # # Example 1: Only inject fail-stop type faults, scope limited to target service
    # generator.start_continuous_injection(
    #     fault_types=[FaultType.FAIL_STOP, FaultType.FAIL_SLOW],
    #     scopes=[PodScope.ALL_PODS],
    #     interval_min=5,
    #     interval_max=30
    # )
    # time.sleep(30)  # Run for 30 seconds then stop
    # Example 2: Manually inject a fail-slow type fault, scope within target namespace
    experiment_name = generator.inject_transient_issue(
        fault_types=[FaultType.FAIL_STOP],
        scopes=[PodScope.ALL_PODS],
        experiment="network-partition"
    )
    
    # Example 3: View active experiments and statistics
    active = generator.get_active_experiments()
    stats = generator.get_statistics()
    print("Currently active experiments:", active)
    print("Statistics:", stats)
    
    # generator.stop_continuous_injection()

    # Example 4: Cleanup all experiments (when program ends)
    generator.cleanup_all_experiments()

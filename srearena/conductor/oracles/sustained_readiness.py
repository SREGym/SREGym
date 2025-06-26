import time
from srearena.conductor.oracles.base import Oracle


class SustainedReadinessOracle(Oracle):

    importance = 1.0

    def __init__(self, problem, buffer_period=10, sustained_period=60, check_interval=0.5):
        super().__init__(problem)
        self.buffer_period = buffer_period
        self.sustained_period = sustained_period
        self.check_interval = check_interval

    def evaluate(self) -> dict:
        print("== Sustained Readiness Evaluation ==")
        
        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        
        print(f"⏳ Waiting up to {self.buffer_period}s for all pods to become ready...")
        start_time = time.time()
        all_ready_start = None
        
        while time.time() - start_time < self.buffer_period:
            if self._check_all_pods_ready(kubectl, namespace):
                all_ready_start = time.time()
                print(f"✅ All pods ready after {all_ready_start - start_time:.1f}s")
                break
            time.sleep(self.check_interval)
        
        if all_ready_start is None:
            print(f"❌ All the pods did not become ready within {self.buffer_period}s buffer period")
            return {"success": False}
        

        print(f"⏱️  Monitoring pods for {self.sustained_period}s sustained readiness...")
        monitoring_start = time.time()
        
        while time.time() - monitoring_start < self.sustained_period:
            elapsed = time.time() - monitoring_start
             
            if not self._check_all_pods_ready(kubectl, namespace, verbose=True):
                print(f"❌ Pod readiness check failed after {elapsed:.1f}s of monitoring")
                return {"success": False}
            
            if int(elapsed) % 10 == 0 and elapsed > 0:
                print(f"🚧 Pods still ready after {int(elapsed)}s...")
            
            time.sleep(self.check_interval)
        
        print(f"✅ All pods remained ready for the full {self.sustained_period}s period!")
        return {"success": True}
    
    def _check_all_pods_ready(self, kubectl, namespace, verbose=False) -> bool:
        try:
            pod_list = kubectl.list_pods(namespace)
            all_normal = True

            for pod in pod_list.items:
                if pod.status.phase != "Running":
                    if verbose:
                        print(f"⚠️ Pod {pod.metadata.name} is in phase: {pod.status.phase}")
                    all_normal = False
                    break
                
                for container_status in pod.status.container_statuses:
                    if container_status.state.waiting and container_status.state.waiting.reason:
                        if verbose:
                            print(f"⚠️ Container {container_status.name} is waiting: {container_status.state.waiting.reason}")
                        all_normal = False
                    
                    elif container_status.state.terminated and container_status.state.terminated.reason != "Completed":
                        if verbose:
                            print(f"⚠️ Container {container_status.name} terminated: {container_status.state.terminated.reason}")
                        all_normal = False
                    
                    elif not container_status.ready:
                        if verbose:
                            print(f"⚠️ Container {container_status.name} is not ready")
                        all_normal = False
            
            return all_normal
            
        except Exception as e:
            if verbose:
                print(f"⚠️ Error checking pod readiness: {e}")
            return False 
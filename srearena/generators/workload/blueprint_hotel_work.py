import time
import threading
from datetime import datetime

import yaml
from kubernetes import client, config
from rich.console import Console

from srearena.generators.workload.base import WorkloadEntry
from srearena.generators.workload.stream import StreamWorkloadManager
from srearena.paths import TARGET_MICROSERVICES
from srearena.generators.noise.transient_issues.chaos_injector import ChaosInjector

# Mimicked the Wrk2 class

class BHotelWrk:
    """
    Persistent workload generator
    """

    def __init__(self, tput: int, duration: str, multiplier: int):
        self.tput = tput
        self.duration = duration
        self.multiplier = multiplier

        config.load_kube_config()

    def create_configmap(self, config_name, namespace):
        api_instance = client.CoreV1Api()
        bhotelwrk_job_configmap = TARGET_MICROSERVICES / "BlueprintHotelReservation" / "wlgen" / "wlgen_proc-configmap.yaml"
        with open(bhotelwrk_job_configmap, 'r', encoding='utf-8') as f:
            configmap_template = yaml.safe_load(f)

        configmap_template['data']['TPUT'] = str(self.tput)
        configmap_template['data']['DURATION'] = self.duration
        configmap_template['data']['MULTIPLIER'] = str(self.multiplier)

        try:
            print(f"Checking for existing ConfigMap '{config_name}'...")
            api_instance.delete_namespaced_config_map(name=config_name, namespace=namespace)
            print(f"ConfigMap '{config_name}' deleted.")
        except client.exceptions.ApiException as e:
            if e.status != 404:
                print(f"Error deleting ConfigMap '{config_name}': {e}")
                return

        try:
            print(f"Creating ConfigMap '{config_name}'...")
            api_instance.create_namespaced_config_map(namespace=namespace, body=configmap_template)
            print(f"ConfigMap '{config_name}' created successfully.")
        except client.exceptions.ApiException as e:
            print(f"Error creating ConfigMap '{config_name}': {e}")


    def create_bhotelwrk_job(self, job_name, namespace):
        bhotelwrk_job_yaml = TARGET_MICROSERVICES / "BlueprintHotelReservation" / "wlgen" / "wlgen_proc-job.yaml"
        with open(bhotelwrk_job_yaml, "r") as f:
            job_template = yaml.safe_load(f)

        api_instance = client.BatchV1Api()
        try:
            existing_job = api_instance.read_namespaced_job(name=job_name, namespace=namespace)
            if existing_job:
                print(f"Job '{job_name}' already exists. Deleting it...")
                api_instance.delete_namespaced_job(
                    name=job_name,
                    namespace=namespace,
                    body=client.V1DeleteOptions(propagation_policy="Foreground"),
                )
                self.wait_for_job_deletion(job_name, namespace)
        except client.exceptions.ApiException as e:
            if e.status != 404:
                print(f"Error checking for existing job: {e}")
                return

        try:
            response = api_instance.create_namespaced_job(namespace=namespace, body=job_template)
            print(f"Job created: {response.metadata.name}")
        except client.exceptions.ApiException as e:
            print(f"Error creating job: {e}")

    def start_workload(self,
                       namespace,
                       configmap_name = "bhotelwrk-wlgen-env",
                       job_name = "bhotelwrk-wlgen-job"):

        self.create_configmap(config_name=configmap_name, namespace=namespace)

        self.create_bhotelwrk_job(job_name=job_name, namespace=namespace)

    def stop_workload(self, namespace, job_name="bhotelwrk-wlgen-proc"):

        api_instance = client.BatchV1Api()
        try:
            existing_job = api_instance.read_namespaced_job(name=job_name, namespace=namespace)
            if existing_job:
                print(f"Stopping job '{job_name}'...")
                api_instance.patch_namespaced_job(name=job_name, namespace=namespace, body={"spec": {"suspend": True}})
                time.sleep(5)
        except client.exceptions.ApiException as e:
            if e.status != 404:
                print(f"Error checking for existing job: {e}")
                return

    def wait_for_job_deletion(self, job_name, namespace, sleep=2, max_wait=60):
        """Wait for a Kubernetes Job to be deleted before proceeding."""
        api_instance = client.BatchV1Api()
        console = Console()
        waited = 0

        while waited < max_wait:
            try:
                api_instance.read_namespaced_job(name=job_name, namespace=namespace)
                time.sleep(sleep)
                waited += sleep
            except client.exceptions.ApiException as e:
                if e.status == 404:
                    console.log(f"[bold green]Job '{job_name}' successfully deleted.")
                    return
                else:
                    console.log(f"[red]Error checking job deletion: {e}")
                    raise

        raise TimeoutError(f"[red]Timed out waiting for job '{job_name}' to be deleted.")


class BHotelWrkWorkloadManager(StreamWorkloadManager):
    """
    Wrk2 workload generator for Kubernetes.
    """

    def __init__(self, wrk: BHotelWrk, namespace:str = 'default', job_name:str="bhotelwrk-wlgen-job", CPU_containment: bool = False):
        super().__init__()
        self.wrk = wrk
        self.job_name = job_name
        self.namespace = namespace
        self.CPU_containment = CPU_containment
        config.load_kube_config()
        self.core_v1_api = client.CoreV1Api()
        self.batch_v1_api = client.BatchV1Api() 

        self.log_pool = []

        # different from self.last_log_time, which is the timestamp of the whole entry
        self.last_log_line_time = None

    def create_task(self):
        namespace = self.namespace
        configmap_name = "bhotelwrk-wlgen-env"

        self.wrk.create_configmap(
            config_name=configmap_name,
            namespace=namespace,
        )

        self.wrk.create_bhotelwrk_job(
            job_name=self.job_name,
            namespace=namespace,
        )

    def _parse_log(self, logs: list[str]) -> WorkloadEntry:
        # -----------------------------------------------------------------------
        #   10 requests in 10.00s, 2.62KB read
        #   Non-2xx or 3xx responses: 10

        number = -1
        ok = True

        try:
            start_time = logs[1].split(": ")[1]
            start_time = datetime.strptime(start_time, "%Y-%m-%dT%H:%M:%S.%fZ").timestamp()
            number = int(logs[2].split(": ")[1])
        except Exception as e:
            print(f"Error parsing log: {e}")
            number = 0
            start_time = -1

        return WorkloadEntry(
            time=start_time,
            number=number,
            log="\n".join([log for log in logs[7:]]),
            ok=ok,
        )

    def retrievelog(self) -> list[WorkloadEntry]:
        namespace = self.namespace
        grouped_logs = []
        pods = self.core_v1_api.list_namespaced_pod(namespace, label_selector=f"job-name={self.job_name}")
        if len(pods.items) == 0:
            raise Exception(f"No pods found for job {self.job_name} in namespace {namespace}")

        try:
            logs = self.core_v1_api.read_namespaced_pod_log(pods.items[0].metadata.name, namespace)
            logs = logs.split("\n")
        except Exception as e:
            print(f"Error retrieving logs from {self.job_name} : {e}")
            return []

        extracted_logs = self._extract_target_logs(logs, startlog="Finished all requests", endlog="End of latency distribution")
        grouped_logs.append(self._parse_log(extracted_logs))
        return grouped_logs

    def _extract_target_logs(self, logs: list[str], startlog: str, endlog: str) -> list[str]:
        start_index = None
        end_index = None
        
        for i, log_line in enumerate(logs):
            if startlog in log_line:
                start_index = i
            elif endlog in log_line and start_index is not None:
                end_index = i
                break
        
        if start_index is not None and end_index is not None:
            return logs[start_index:end_index]
        
        return []

    def _schedule_cpu_containment(self):
        """
        Schedule CPU containment injection and recovery based on workload start time.
        """
        if not self.CPU_containment:
            return
            
        # Initialize fault injector
        self.cpu_containment_injector = ChaosInjector(self.namespace)
        
        # Schedule CPU stress injection after 60 seconds
        self.cpu_stress_timer = threading.Timer(60.0, self._inject_cpu_stress)
        self.cpu_stress_timer.start()
        print("CPU stress injection scheduled for 60 seconds after workload start")
        
        # Schedule CPU stress recovery after 90 seconds
        self.cpu_recovery_timer = threading.Timer(90.0, self._recover_cpu_stress)
        self.cpu_recovery_timer.start()
        print("CPU stress recovery scheduled for 90 seconds after workload start")

    def _inject_cpu_stress(self):
        """
        Inject CPU stress using the symptom fault injector.
        """
        try:
            print("Injecting CPU stress...")
            # You may need to adjust deployment_name and microservice based on your setup
            experiment_name = f"cpu-stress-all-pods"
            chaos_experiment = {
                "apiVersion": "chaos-mesh.org/v1alpha1",
                "kind": "StressChaos",
                "metadata": {
                    "name": experiment_name,
                    "namespace": 'chaos-mesh',
                },
                "spec": {
                    "mode": "all",
                    "selector": {
                        "namespaces": [self.namespace],
                    },
                    "stressors": {
                        "cpu": {
                            "workers": 30,
                            "load": 90,
                        }
                    },
                },
            }
            self.cpu_containment_injector.create_chaos_experiment(chaos_experiment, experiment_name)
            start_time = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
            print(f"[{start_time}] Injecting CPU stress...")
            self.current_experiment_name = experiment_name  # Save the current experiment name
            print("CPU stress injection completed")
        except Exception as e:
            print(f"Error injecting CPU stress: {e}")

    def _recover_cpu_stress(self):
        """
        Recover from CPU stress by deleting the ChaosMesh experiment.
        """
        try:
            print("Recovering from CPU stress...")
            
            if hasattr(self, 'current_experiment_name'):
                self.cpu_containment_injector.delete_chaos_experiment(self.current_experiment_name)
                print("CPU stress recovery completed for all pods")
            else:
                print("No active CPU stress experiment found")
                
        except Exception as e:
            print(f"Error recovering from CPU stress: {e}")

    def start(self):
        print("== Start Workload ==")
        self.create_task()
        self._schedule_cpu_containment()

    def stop(self):
        print("== Stop Workload ==")
        self.wrk.stop_workload(job_name=self.job_name, namespace=self.namespace)

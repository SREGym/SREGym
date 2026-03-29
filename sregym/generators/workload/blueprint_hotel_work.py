import logging
import time
from datetime import datetime

import yaml
from kubernetes import client, config
from rich.console import Console

from sregym.generators.noise.impl.stress_injector import ChaosInjector
from sregym.generators.workload.base import WorkloadEntry
from sregym.generators.workload.stream import StreamWorkloadManager
from sregym.paths import TARGET_MICROSERVICES

# Mimicked the Wrk2 class

logger = logging.getLogger("all.infra.workload")
logger.propagate = True
logger.setLevel(logging.DEBUG)


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
        bhotelwrk_job_configmap = (
            TARGET_MICROSERVICES / "BlueprintHotelReservation" / "wlgen" / "wlgen_proc-configmap.yaml"
        )
        with open(bhotelwrk_job_configmap, encoding="utf-8") as f:
            configmap_template = yaml.safe_load(f)

        configmap_template["data"]["TPUT"] = str(self.tput)
        configmap_template["data"]["DURATION"] = self.duration
        configmap_template["data"]["MULTIPLIER"] = str(self.multiplier)

        try:
            logger.info(f"Checking for existing ConfigMap '{config_name}'...")
            api_instance.delete_namespaced_config_map(name=config_name, namespace=namespace)
            logger.info(f"ConfigMap '{config_name}' deleted.")
        except client.exceptions.ApiException as e:
            if e.status != 404:
                logger.error(f"Error deleting ConfigMap '{config_name}': {e}")
                return

        try:
            logger.info(f"Creating ConfigMap '{config_name}'...")
            api_instance.create_namespaced_config_map(namespace=namespace, body=configmap_template)
            logger.info(f"ConfigMap '{config_name}' created successfully.")
        except client.exceptions.ApiException as e:
            logger.error(f"Error creating ConfigMap '{config_name}': {e}")

    def create_bhotelwrk_deployment(self, deployment_name, namespace):
        bhotelwrk_deployment_yaml = (
            TARGET_MICROSERVICES / "BlueprintHotelReservation" / "wlgen" / "wlgen_proc-deployment.yaml"
        )
        with open(bhotelwrk_deployment_yaml) as f:
            deployment_template = yaml.safe_load(f)

        api_instance = client.AppsV1Api()
        try:
            existing = api_instance.read_namespaced_deployment(name=deployment_name, namespace=namespace)
            if existing:
                logger.info(f"Deployment '{deployment_name}' already exists. Deleting it...")
                api_instance.delete_namespaced_deployment(
                    name=deployment_name,
                    namespace=namespace,
                    body=client.V1DeleteOptions(propagation_policy="Foreground"),
                )
        except client.exceptions.ApiException as e:
            if e.status != 404:
                logger.error(f"Error checking for existing deployment: {e}")
                return

        try:
            response = api_instance.create_namespaced_deployment(namespace=namespace, body=deployment_template)
            logger.info(f"Deployment created: {response.metadata.name}")
        except client.exceptions.ApiException as e:
            logger.error(f"Error creating deployment: {e}")

    def delete_bhotelwrk_deployment(self, deployment_name, namespace):
        api_instance = client.AppsV1Api()
        try:
            api_instance.delete_namespaced_deployment(
                name=deployment_name,
                namespace=namespace,
                body=client.V1DeleteOptions(propagation_policy="Foreground"),
            )
            logger.info(f"Deployment '{deployment_name}' deleted.")
        except client.exceptions.ApiException as e:
            if e.status != 404:
                logger.error(f"Error deleting deployment '{deployment_name}': {e}")

    def create_bhotelwrk_job(self, job_name, namespace):
        bhotelwrk_job_yaml = TARGET_MICROSERVICES / "BlueprintHotelReservation" / "wlgen" / "wlgen_proc-job.yaml"
        with open(bhotelwrk_job_yaml) as f:
            job_template = yaml.safe_load(f)

        api_instance = client.BatchV1Api()
        try:
            existing_job = api_instance.read_namespaced_job(name=job_name, namespace=namespace)
            if existing_job:
                logger.info(f"Job '{job_name}' already exists. Deleting it...")
                api_instance.delete_namespaced_job(
                    name=job_name,
                    namespace=namespace,
                    body=client.V1DeleteOptions(propagation_policy="Foreground"),
                )
                self.wait_for_job_deletion(job_name, namespace)
        except client.exceptions.ApiException as e:
            if e.status != 404:
                logger.error(f"Error checking for existing job: {e}")
                return

        try:
            response = api_instance.create_namespaced_job(namespace=namespace, body=job_template)
            logger.info(f"Job created: {response.metadata.name}")
        except client.exceptions.ApiException as e:
            logger.error(f"Error creating job: {e}")

    def start_workload(self, namespace, configmap_name="bhotelwrk-wlgen-env", job_name="bhotelwrk-wlgen-job"):
        self.create_configmap(config_name=configmap_name, namespace=namespace)

        self.create_bhotelwrk_job(job_name=job_name, namespace=namespace)

    def stop_workload(self, namespace, job_name="bhotelwrk-wlgen-proc"):
        api_instance = client.BatchV1Api()
        try:
            existing_job = api_instance.read_namespaced_job(name=job_name, namespace=namespace)
            if existing_job:
                logger.info(f"Stopping job '{job_name}'...")
                api_instance.patch_namespaced_job(name=job_name, namespace=namespace, body={"spec": {"suspend": True}})
                time.sleep(5)
        except client.exceptions.ApiException as e:
            if e.status != 404:
                logger.error(f"Error checking for existing job: {e}")
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

    def __init__(
        self,
        wrk: BHotelWrk,
        namespace: str = "default",
        job_name: str = "bhotelwrk-wlgen-job",
        CPU_containment: bool = False,
        continuous: bool = False,
        deployment_name: str = "bhotelwrk-wlgen",
        apply_capacity_restraint: bool = True,
    ):
        super().__init__()
        self.wrk = wrk
        self.job_name = job_name
        self.namespace = namespace
        self.CPU_containment = CPU_containment
        self.continuous = continuous
        self.deployment_name = deployment_name
        self.apply_capacity_restraint = apply_capacity_restraint
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

        if self.continuous:
            self.wrk.create_bhotelwrk_deployment(
                deployment_name=self.deployment_name,
                namespace=namespace,
            )
        else:
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
            logger.error(f"Error parsing log: {e}")
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
            logger.error(f"Error retrieving logs from {self.job_name} : {e}")
            return []

        extracted_logs = self._extract_target_logs(
            logs, startlog="Finished all requests", endlog="End of latency distribution"
        )
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

    def _run_cpu_containment_sequence(self):
        """
        Synchronously execute the full capacity-decrease trigger sequence:
          t+0s  : workload already running
          t+60s : inject NetworkChaos + CPU stress DaemonSet (trigger)
          t+90s : remove trigger, apply permanent capacity restraint
        Blocks until the metastable state is fully established.
        """
        if not self.CPU_containment:
            return

        self.cpu_containment_injector = ChaosInjector(self.namespace)

        logger.info("Waiting 60s before injecting capacity-decrease trigger...")
        time.sleep(60)
        self._inject_cpu_stress()

        logger.info("Waiting 30s before removing trigger and applying capacity restraint...")
        time.sleep(30)
        self._recover_cpu_stress()

    def _inject_cpu_stress(self):
        """
        Inject both network latency and CPU stress to trigger and sustain the retry storm.
        - NetworkChaos (100ms): pushes gRPC calls above the 50ms timeout → triggers retries → 31x request flood
        - StressChaos (CPU): saturates server capacity so the flood causes queueing → latency spike visible in Prometheus
        """
        try:
            print(
                "[Step 3a] Injecting 100ms network latency — gRPC calls will exceed 50ms timeout, triggering retries → 31x request flood..."
            )
            logger.info("Injecting network latency...")
            network_experiment_name = "network-latency-all-pods"
            network_chaos = {
                "apiVersion": "chaos-mesh.org/v1alpha1",
                "kind": "NetworkChaos",
                "metadata": {
                    "name": network_experiment_name,
                    "namespace": self.namespace,
                },
                "spec": {
                    "action": "delay",
                    "mode": "all",
                    "selector": {
                        "namespaces": [self.namespace],
                    },
                    "delay": {
                        "latency": "100ms",
                        "correlation": "0",
                        "jitter": "0ms",
                    },
                    "direction": "to",
                },
            }
            self.cpu_containment_injector.create_chaos_experiment(network_chaos, network_experiment_name)

            print(
                "[Step 3a] Deploying CPU stress DaemonSet on all nodes — one stress pod per node regardless of scheduling..."
            )
            logger.info("Deploying CPU stress DaemonSet...")
            self._deploy_cpu_stress_daemonset()

            start_time = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
            self.current_experiment_names = [network_experiment_name]
            logger.info(f"[{start_time}] Network latency + CPU stress DaemonSet injection completed")
            print(
                f"[Step 3a] Done — network latency + CPU stress active on all nodes at {start_time}. Watch Prometheus p99 latency for spike above 50ms."
            )
        except Exception as e:
            logger.error(f"Error injecting chaos experiments: {e}")

    def _deploy_cpu_stress_daemonset(self):
        apps_v1 = client.AppsV1Api()
        daemonset_name = "cpu-stress-daemon"

        try:
            apps_v1.delete_namespaced_daemon_set(
                name=daemonset_name,
                namespace=self.namespace,
                body=client.V1DeleteOptions(propagation_policy="Foreground"),
            )
            logger.info(f"Deleted existing DaemonSet '{daemonset_name}'")
        except client.exceptions.ApiException as e:
            if e.status != 404:
                logger.warning(f"Error deleting existing stress DaemonSet: {e}")

        daemonset_body = {
            "apiVersion": "apps/v1",
            "kind": "DaemonSet",
            "metadata": {
                "name": daemonset_name,
                "namespace": self.namespace,
            },
            "spec": {
                "selector": {"matchLabels": {"app": daemonset_name}},
                "template": {
                    "metadata": {"labels": {"app": daemonset_name}},
                    "spec": {
                        "tolerations": [{"operator": "Exists"}],
                        "containers": [
                            {
                                "name": "stress",
                                "image": "polinux/stress",
                                "command": ["/bin/sh", "-c"],
                                "args": ["stress --cpu $(nproc)"],
                            }
                        ],
                    },
                },
            },
        }
        apps_v1.create_namespaced_daemon_set(namespace=self.namespace, body=daemonset_body)
        self.cpu_stress_daemonset_name = daemonset_name
        logger.info(f"CPU stress DaemonSet '{daemonset_name}' deployed — one stress pod per node")

    def _delete_cpu_stress_daemonset(self):
        if not hasattr(self, "cpu_stress_daemonset_name"):
            return
        apps_v1 = client.AppsV1Api()
        try:
            apps_v1.delete_namespaced_daemon_set(
                name=self.cpu_stress_daemonset_name,
                namespace=self.namespace,
                body=client.V1DeleteOptions(propagation_policy="Foreground"),
            )
            logger.info(f"CPU stress DaemonSet '{self.cpu_stress_daemonset_name}' deleted")
        except client.exceptions.ApiException as e:
            if e.status != 404:
                logger.error(f"Error deleting CPU stress DaemonSet: {e}")

    def _recover_cpu_stress(self):
        """
        Remove the external trigger (NetworkChaos + CPU stress DaemonSet), then apply a
        permanent capacity restraint via ResourceQuota + LimitRange so the retry-amplified
        load (31x) continues to exceed service capacity — sustaining the metastable storm
        until a hard reboot (recover_fault) removes the restraint.
        """
        try:
            print("[Step 3b] Removing network latency + CPU stress DaemonSet (trigger)...")
            logger.info("Recovering chaos experiments...")

            if hasattr(self, "current_experiment_names"):
                for experiment_name in self.current_experiment_names:
                    self.cpu_containment_injector.delete_chaos_experiment(experiment_name)
                    logger.info(f"Deleted chaos experiment: {experiment_name}")
            self._delete_cpu_stress_daemonset()

            if self.apply_capacity_restraint:
                print(
                    "[Step 3b] Trigger removed — applying permanent capacity restraint to sustain metastable storm..."
                )
                self._apply_capacity_restraint()

            recover_time = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
            print(
                f"[Step 3b] Done at {recover_time}. Trigger removed; capacity constrained. "
                "Retry storm (31x amplification) will sustain latency > 50ms until hard reboot."
            )

        except Exception as e:
            logger.error(f"Error in _recover_cpu_stress: {e}")

    def _apply_capacity_restraint(self):
        """
        Apply a namespace ResourceQuota + LimitRange that caps per-container CPU to 200m,
        then rolling-restart all deployments so pods inherit the limit.

        After the trigger is removed, the 31x retry-amplified load still exceeds the
        constrained capacity (200m/container), keeping response times above the 50ms
        gRPC timeout and sustaining the retry storm indefinitely.

        Hard reboot (recover_fault) must delete these objects and restart pods to recover.
        """
        # 1. ResourceQuota — caps total CPU requestable in the namespace
        quota_body = client.V1ResourceQuota(
            metadata=client.V1ObjectMeta(name="capacity-restraint"),
            spec=client.V1ResourceQuotaSpec(hard={"requests.cpu": "4", "limits.cpu": "8"}),
        )
        try:
            self.core_v1_api.delete_namespaced_resource_quota("capacity-restraint", self.namespace)
        except client.exceptions.ApiException as e:
            if e.status != 404:
                logger.warning(f"Error deleting existing ResourceQuota: {e}")
        self.core_v1_api.create_namespaced_resource_quota(self.namespace, quota_body)
        logger.info("ResourceQuota 'capacity-restraint' applied")

        # 2. LimitRange — assigns default CPU limit of 200m to containers without explicit limits
        limit_range_body = client.V1LimitRange(
            metadata=client.V1ObjectMeta(name="capacity-restraint"),
            spec=client.V1LimitRangeSpec(
                limits=[
                    client.V1LimitRangeItem(
                        type="Container",
                        default={"cpu": "200m"},
                        default_request={"cpu": "100m"},
                    )
                ]
            ),
        )
        try:
            self.core_v1_api.delete_namespaced_limit_range("capacity-restraint", self.namespace)
        except client.exceptions.ApiException as e:
            if e.status != 404:
                logger.warning(f"Error deleting existing LimitRange: {e}")
        self.core_v1_api.create_namespaced_limit_range(self.namespace, limit_range_body)
        logger.info("LimitRange 'capacity-restraint' applied")

        # 3. Rolling restart all deployments so pods come up with the 200m CPU limit
        apps_v1 = client.AppsV1Api()
        deployments = apps_v1.list_namespaced_deployment(self.namespace)
        restart_ts = datetime.now().isoformat()
        for dep in deployments.items:
            patch = {
                "spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": restart_ts}}}}
            }
            apps_v1.patch_namespaced_deployment(dep.metadata.name, self.namespace, patch)
        logger.info(f"Rolling restart triggered for all deployments in {self.namespace}")
        print(
            f"[Step 3b] {len(deployments.items)} deployments restarted with 200m CPU limit — "
            "capacity permanently restrained."
        )

    def start(self):
        logger.info("Start Workload with Blueprint Hotel Workload Manager")
        self.create_task()
        self._run_cpu_containment_sequence()

    def stop(self):
        logger.info("Stop Workload with Blueprint Hotel Workload Manager")
        if self.continuous:
            self.wrk.delete_bhotelwrk_deployment(deployment_name=self.deployment_name, namespace=self.namespace)
        else:
            self.wrk.stop_workload(job_name=self.job_name, namespace=self.namespace)

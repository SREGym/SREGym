import asyncio
import time

import yaml
from kubernetes import client, config
from kubernetes.client import V1JobStatus
from pydantic import ConfigDict, Field

from clients.stratus.weak_oracles.base_oracle import BaseOracle, OracleResult

# from aiopslab.generators.workload.wrk import Wrk
from sregym.paths import BASE_DIR, TARGET_MICROSERVICES
from sregym.service.apps.base import Application
from sregym.service.kubectl import KubeCtl

# from sregym.generators.workload.wrk2 import Wrk2 as Wrk


class Wrk:
    def __init__(self, rate, dist="norm", connections=2, duration=6, threads=2, latency=True):
        self.rate = rate
        self.dist = dist
        self.connections = connections
        self.duration = duration
        self.threads = threads
        self.latency = latency

        config.load_kube_config()

        self.kubectl = KubeCtl()

    def create_configmap(self, name, namespace, payload_script_path):
        with open(payload_script_path, "r") as script_file:
            script_content = script_file.read()

        configmap_body = client.V1ConfigMap(
            metadata=client.V1ObjectMeta(name=name),
            data={payload_script_path.name: script_content},
        )

        api_instance = client.CoreV1Api()
        try:
            print(f"Checking for existing ConfigMap '{name}'...")
            api_instance.delete_namespaced_config_map(name=name, namespace=namespace)
            print(f"ConfigMap '{name}' deleted.")
        except client.exceptions.ApiException as e:
            if e.status != 404:
                print(f"Error deleting ConfigMap '{name}': {e}")
                return

        try:
            print(f"Creating ConfigMap '{name}'...")
            api_instance.create_namespaced_config_map(namespace=namespace, body=configmap_body)
            print(f"ConfigMap '{name}' created successfully.")
        except client.exceptions.ApiException as e:
            print(f"Error creating ConfigMap '{name}': {e}")

    def create_wrk_job(self, job_name, namespace, payload_script, url):
        wrk_job_yaml = BASE_DIR / "generators" / "workload" / "wrk-job-template.yaml"
        with open(wrk_job_yaml, "r") as f:
            job_template = yaml.safe_load(f)

        job_template["metadata"]["name"] = job_name
        container = job_template["spec"]["template"]["spec"]["containers"][0]
        container["args"] = [
            "wrk",
            "-D",
            self.dist,
            "-t",
            str(self.threads),
            "-c",
            str(self.connections),
            "-d",
            f"{self.duration}s",
            "-L",
            "-s",
            f"/scripts/{payload_script}",
            url,
            "-R",
            str(self.rate),
        ]

        if self.latency:
            container["args"].append("--latency")

        job_template["spec"]["template"]["spec"]["volumes"] = [
            {
                "name": "wrk2-scripts",
                "configMap": {"name": "wrk2-payload-script"},
            }
        ]
        job_template["spec"]["template"]["spec"]["containers"][0]["volumeMounts"] = [
            {
                "name": "wrk2-scripts",
                "mountPath": f"/scripts/{payload_script}",
                "subPath": payload_script,
            }
        ]

        api_instance = client.BatchV1Api()
        try:
            existing_job = api_instance.read_namespaced_job(name=job_name, namespace=namespace)
            if existing_job:
                print(f"Job '{job_name}' already exists. Deleting it...")
                api_instance.delete_namespaced_job(
                    name=job_name, namespace=namespace, body=client.V1DeleteOptions(propagation_policy="Foreground")
                )
                time.sleep(5)
        except client.exceptions.ApiException as e:
            if e.status != 404:
                print(f"Error checking for existing job: {e}")
                return

        try:
            response = api_instance.create_namespaced_job(namespace=namespace, body=job_template)
            print(f"Job created: {response.metadata.name}")
        except client.exceptions.ApiException as e:
            print(f"Error creating job: {e}")
            return

        try:
            while True:
                job_status = api_instance.read_namespaced_job_status(name=job_name, namespace=namespace)
                if job_status.status.ready:
                    print("Job completed successfully.")
                    break
                elif job_status.status.failed:
                    print("Job failed.")
                    break
                time.sleep(5)
        except client.exceptions.ApiException as e:
            print(f"Error monitoring job: {e}")

    def start_workload(self, payload_script, url):
        namespace = "default"
        configmap_name = "wrk2-payload-script"

        self.create_configmap(name=configmap_name, namespace=namespace, payload_script_path=payload_script)

        self.create_wrk_job(job_name="wrk2-job", namespace=namespace, payload_script=payload_script.name, url=url)


class WorkloadOracle(BaseOracle):
    passable: bool = Field(default=True)

    model_config = ConfigDict(arbitrary_types_allowed=True)
    app: Application = Field(default=None, description="Start workload")
    core_v1_api: client.CoreV1Api = Field(default=None, description="Kubernetes CoreV1 API client")
    batch_v1_api: client.BatchV1Api = Field(default=None, description="Kubernetes BatchV1 API client")
    wrk: Wrk = Field(default=None, description="Wrk workload generator")

    def __init__(self, app: Application):
        super().__init__()
        self.app = app

        config.load_kube_config()
        self.core_v1_api = client.CoreV1Api()
        self.batch_v1_api = client.BatchV1Api()
        self.kubectl = KubeCtl()

    def get_job_logs(self, job_name, namespace):
        """Retrieve the logs of a specified job within a namespace."""

        pods = self.core_v1_api.list_namespaced_pod(namespace, label_selector=f"job-name={job_name}")
        print(
            pods.items[0].metadata.name,
            self.core_v1_api.read_namespaced_pod_log(pods.items[0].metadata.name, namespace),
        )
        if len(pods.items) == 0:
            raise Exception(f"No pods found for job {job_name} in namespace {namespace}")
        return self.core_v1_api.read_namespaced_pod_log(pods.items[0].metadata.name, namespace)

    def get_base_url(self):
        # these are assumed to be initialized within the specific app
        endpoint = self.kubectl.get_cluster_ip(self.app.frontend_service, self.app.namespace)
        return f"http://{endpoint}:{self.app.frontend_port}"

    def get_workloads(self, app_type):
        if app_type == "Social Network":
            base_dir = TARGET_MICROSERVICES / "socialNetwork/wrk2/scripts/social-network"
            return [
                {"payload_script": base_dir / "compose-post.lua", "url": "/wrk2-api/post/compose"},
                {"payload_script": base_dir / "read-home-timeline.lua", "url": "/wrk2-api/home-timeline/read"},
                {"payload_script": base_dir / "read-user-timeline.lua", "url": "/wrk2-api/user-timeline/read"},
            ]
        elif app_type == "Hotel Reservation":
            base_dir = TARGET_MICROSERVICES / "hotelReservation/wrk2/scripts/hotel-reservation"
            return [
                {"payload_script": base_dir / "mixed-workload_type_1.lua", "url": ""},
            ]
        else:
            raise Exception(f"Unknown app type: {app_type}")

    @staticmethod
    def is_job_completed(job_status: V1JobStatus) -> bool:
        if hasattr(job_status, "conditions") and job_status.conditions is not None:
            for condition in job_status.conditions:
                if condition.type == "Complete" and condition.status == "True":
                    return True
        return False

    async def get_workload_result(self, job_name):
        await self.kubectl.wait_for_job_completion(job_name=job_name, namespace="default")

        namespace = "default"

        logs = None
        try:
            logs = self.get_job_logs(
                job_name=job_name,
                namespace=namespace,
            )
            logs = "\n".join(logs.split("\n"))
        except Exception as e:
            return f"Workload Generator Error: {e}"

        return logs

    def start_workload(self, payload_script, url, job_name):
        namespace = "default"
        configmap_name = "wrk2-payload-script"

        self.wrk.create_configmap(name=configmap_name, namespace=namespace, payload_script_path=payload_script)

        self.wrk.create_wrk_job(job_name=job_name, namespace=namespace, payload_script=payload_script.name, url=url)

    async def validate(self) -> OracleResult:
        print("Testing workload generator...", flush=True)
        self.wrk = Wrk(rate=10, dist="exp", connections=2, duration=10, threads=2)

        result = {"success": True, "issues": []}

        base_url = self.get_base_url()

        for runid, run_config in enumerate(self.get_workloads(self.app.name)):
            payload_script = run_config["payload_script"]
            url = base_url + run_config["url"]
            job_name = f"wrk2-job-{runid}"

            self.start_workload(payload_script, url, job_name)
            wrk_result = await self.get_workload_result(job_name)
            if (
                "Workload Generator Error:" in wrk_result
                or "Requests/sec:" not in wrk_result
                or "Transfer/sec:" not in wrk_result
            ):
                result["issues"].append("Workload Generator Error")
                result["success"] = False
                break
            elif "Non-2xx or 3xx responses:" in wrk_result:
                issue = ""
                for line in wrk_result.split("\n"):
                    if "Non-2xx or 3xx responses:" in line:
                        issue = line
                        break
                result["issues"].append(issue)
                result["success"] = False
                break

        return OracleResult(
            success=result["success"],
            issues=[str(result)],
        )

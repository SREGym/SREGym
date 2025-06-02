"""Interface to the wrk workload generator."""

import time

import yaml
from kubernetes import client, config
from rich.console import Console

from srearena.paths import BASE_DIR


class Wrk:
    def __init__(self, rate, dist="norm", connections=2, duration=6, threads=2, latency=True):
        self.rate = rate
        self.dist = dist
        self.connections = connections
        self.duration = duration
        self.threads = threads
        self.latency = latency

        config.load_kube_config()

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

    def wait_for_job_deletion(self, job_name, namespace, sleep=2, max_wait=60):
        """Wait for a Kubernetes Job to be deleted before proceeding."""
        api_instance = client.BatchV1Api()
        console = Console()
        waited = 0

        with console.status(f"[bold yellow]Waiting for job '{job_name}' to be deleted..."):
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

    def create_wrk_job(self, job_name, namespace, payload_script, url):
        wrk_job_yaml = BASE_DIR / "generators" / "workload" / "wrk-job-template.yaml"
        with open(wrk_job_yaml, "r") as f:
            job_template = yaml.safe_load(f)

        # Configure job...
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

        # Set volumes and mounts
        job_template["spec"]["template"]["spec"]["volumes"] = [
            {"name": "wrk2-scripts", "configMap": {"name": "wrk2-payload-script"}}
        ]
        container["volumeMounts"] = [
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

    def start_workload(self, payload_script, url):
        namespace = "default"
        configmap_name = "wrk2-payload-script"

        self.create_configmap(name=configmap_name, namespace=namespace, payload_script_path=payload_script)

        self.create_wrk_job(
            job_name="wrk2-job",
            namespace=namespace,
            payload_script=payload_script.name,
            url=url,
        )

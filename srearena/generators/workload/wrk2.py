import math
import textwrap
import time
from datetime import datetime
from pathlib import Path

import yaml
from kubernetes import client, config, stream
from rich.console import Console

from srearena.generators.workload.stream import STREAM_WORKLOAD_EPS, StreamWorkloadManager, WorkloadEntry
from srearena.paths import BASE_DIR


class Wrk2:
    """
    Persistent workload generator
    """

    def __init__(self, rate, dist="norm", connections=2, duration=6, threads=2, latency=True):
        self.rate = rate
        self.dist = dist
        self.connections = connections
        self.duration = duration
        self.threads = threads
        self.latency = latency

        config.load_kube_config()

    def create_configmap(self, name, namespace, payload_script_path, url):
        with open(payload_script_path, "r") as script_file:
            script_content = script_file.read()

        workload_script = f"""
        #!/bin/bash
        round=0
        while true; do
            echo "Running wrk2 on round #${{round}}"
            round=$((round + 1))

            wrk -D {self.dist} \\
            -t {str(self.threads)} \\
            -c {str(self.connections)} \\
            -d {self.duration}s \\
            -s /scripts/{payload_script_path.name} \\
            {url} \\
            -R {str(self.rate)} \\
            -L {"--latency" if self.latency else ""}
            sleep 1
        done
        """

        workload_script = textwrap.dedent(workload_script).strip()

        configmap_body = client.V1ConfigMap(
            metadata=client.V1ObjectMeta(name=name),
            data={
                payload_script_path.name: script_content,
                "wrk2-workload.sh": workload_script,
            },
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

    def create_wrk_job(self, job_name, namespace, payload_script):
        wrk_job_yaml = BASE_DIR / "generators" / "workload" / "wrk-job-template.yaml"
        with open(wrk_job_yaml, "r") as f:
            job_template = yaml.safe_load(f)

        job_template["metadata"]["name"] = job_name
        container = job_template["spec"]["template"]["spec"]["containers"][0]
        container["args"] = ["/bin/bash", "/scripts/wrk2-workload.sh"]

        job_template["spec"]["template"]["spec"]["volumes"] = [
            {
                "name": "wrk2-scripts",
                "configMap": {"name": "wrk2-payload-script"},
            }
        ]
        container["volumeMounts"] = [
            {
                "name": "wrk2-scripts",
                "mountPath": f"/scripts/{payload_script}",
                "subPath": payload_script,
            },
            {
                "name": "wrk2-scripts",
                "mountPath": f"/scripts/wrk2-workload.sh",
                "subPath": "wrk2-workload.sh",
            },
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

        self.create_wrk_job(job_name="wrk2-job", namespace=namespace, payload_script=payload_script.name, url=url)

    def stop_workload(self, job_name="wrk2-job"):
        namespace = "default"

        api_instance = client.BatchV1Api()
        try:
            existing_job = api_instance.read_namespaced_job(name=job_name, namespace=namespace)
            if existing_job:
                print(f"Stopping job '{job_name}'...")
                # @daklqw: I think there might be a better way
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


class Wrk2WorkloadManager(StreamWorkloadManager):
    """
    Wrk2 workload generator for Kubernetes.
    """

    def __init__(self, wrk: Wrk2, payload_script: Path, url, job_name="wrk2-job"):
        super().__init__()
        self.wrk = wrk
        self.payload_script = payload_script
        self.url = url
        self.job_name = job_name

        config.load_kube_config()
        self.core_v1_api = client.CoreV1Api()
        self.batch_v1_api = client.BatchV1Api()

        self.log_pool = []

        # different from self.last_log_time, which is the timestamp of the whole entry
        self.last_log_line_time = None

    def create_task(self):
        namespace = "default"
        configmap_name = "wrk2-payload-script"

        self.wrk.create_configmap(
            name=configmap_name,
            namespace=namespace,
            payload_script_path=self.payload_script,
            url=self.url,
        )

        self.wrk.create_wrk_job(
            job_name=self.job_name,
            namespace=namespace,
            payload_script=self.payload_script.name,
        )

    def _parse_log(self, logs: list[tuple[str, str]]) -> WorkloadEntry:
        # -----------------------------------------------------------------------
        #   10 requests in 10.00s, 2.62KB read
        #   Non-2xx or 3xx responses: 10

        number = -1
        ok = True

        try:
            start_time = logs[0]["time"][0:26] + "Z"  # Convert to ISO 8601 format
            start_time = datetime.strptime(start_time, "%Y-%m-%dT%H:%M:%S.%fZ").timestamp()

            for i, part in enumerate(logs):
                log = part["content"]
                if "-" * 35 in log and "requests in" in logs[i + 1]["content"]:
                    parts = logs[i + 1]["content"].split(" ")
                    for j, part in enumerate(parts):
                        if part != "":
                            number = parts[j]
                            assert j + 1 < len(parts) and parts[j + 1] == "requests"
                            break
                if "Non-2xx or 3xx responses" in log:
                    ok = False

            number = int(number)
        except Exception as e:
            print(f"Error parsing log: {e}")
            number = 0
            start_time = -1

        return WorkloadEntry(
            time=start_time,
            number=number,
            log="\n".join([part["content"] for part in logs]),
            ok=ok,
        )

    def retrievelog(self, start_time: float | None = None) -> list[WorkloadEntry]:
        namespace = "default"

        pods = self.core_v1_api.list_namespaced_pod(namespace, label_selector=f"job-name={self.job_name}")
        if len(pods.items) == 0:
            raise Exception(f"No pods found for job {self.job_name} in namespace {namespace}")

        kwargs = {
            "timestamps": True,
        }
        if start_time is not None:
            # Get the current time inside the pod by executing 'date +%s' in the pod
            resp = stream.stream(
                self.core_v1_api.connect_get_namespaced_pod_exec,
                name=pods.items[0].metadata.name,
                namespace=namespace,
                command=["date", "-Ins"],
                stderr=True,
                stdin=False,
                stdout=True,
                tty=False,
            )

            # 2025-01-01T12:34:56,123456
            shorter = resp.strip()[:26]
            pod_current_time = datetime.strptime(shorter, "%Y-%m-%dT%H:%M:%S,%f").timestamp()
            # Use the difference between pod's current time and requested start_time
            kwargs["since_seconds"] = math.ceil(pod_current_time - start_time) + STREAM_WORKLOAD_EPS

        try:
            logs = self.core_v1_api.read_namespaced_pod_log(pods.items[0].metadata.name, namespace, **kwargs)
            logs = logs.split("\n")
        except Exception as e:
            print(f"Error retrieving logs from {self.job_name} : {e}")
            return []

        for log in logs:
            timestamp = log[0:30]
            content = log[31:]

            # last_log_line_time: in string format, e.g. "2025-01-01T12:34:56.789012345Z"
            if self.last_log_line_time is not None and timestamp <= self.last_log_line_time:
                continue

            self.last_log_line_time = timestamp
            self.log_pool.append(dict(time=timestamp, content=content))

        # End pattern is:
        #   - Requests/sec:
        #   - Transfer/sec:

        grouped_logs = []

        last_end = 0
        for i, log in enumerate(self.log_pool):
            if (i > 0 and "Requests/sec:" in self.log_pool[i - 1]["content"]) and "Transfer/sec:" in log["content"]:
                result = self._parse_log(self.log_pool[last_end : i + 1])
                grouped_logs.append(result)
                last_end = i + 1

        self.log_pool = self.log_pool[last_end:]

        return grouped_logs

    def start(self):
        print("== Start Workload ==")
        self.create_task()

    def stop(self):
        print("== Stop Workload ==")
        self.wrk.stop_workload(job_name=self.job_name)

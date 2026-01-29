import json
import logging
import math
import time
from datetime import datetime

import yaml
from kubernetes import client, config, stream

from sregym.generators.workload.base import WorkloadEntry
from sregym.generators.workload.stream import STREAM_WORKLOAD_EPS, StreamWorkloadManager
from sregym.paths import BASE_DIR
from sregym.service.kubectl import KubeCtl

logger = logging.getLogger("all.infra.workload")
logger.propagate = True
logger.setLevel(logging.DEBUG)


class LocustWorkloadManager(StreamWorkloadManager):
    def __init__(self, namespace: str, locust_url: str):
        super().__init__()

        self.namespace = namespace
        self.locust_url = locust_url

        self.log_pool = []
        self.last_log_line_time = None

        config.load_kube_config()
        self.core_v1_api = client.CoreV1Api()

        self.kubectl = KubeCtl()

    def remove_fetcher(self):
        try:
            pods = self.core_v1_api.list_namespaced_pod(namespace=self.namespace, label_selector="app=locust-fetcher")
            if pods.items:
                print("Found locust-fetcher pod, removing it...")
                self.core_v1_api.delete_namespaced_pod(
                    name=pods.items[0].metadata.name,
                    namespace=self.namespace,
                    body=client.V1DeleteOptions(grace_period_seconds=0, propagation_policy="Background"),
                )
                while True:
                    time.sleep(5)
                    pods = self.core_v1_api.list_namespaced_pod(
                        namespace=self.namespace, label_selector="app=locust-fetcher"
                    )
                    if not pods.items:
                        break
        except client.exceptions.ApiException as e:
            if e.status != 404:
                print(f"Error removing pod: {e}")
                return

    def create_fetcher(self):
        self.remove_fetcher()

        wrk_job_yaml = BASE_DIR / "generators" / "workload" / "locust-fetcher-template.yaml"
        with open(wrk_job_yaml, "r") as f:
            job_template = yaml.safe_load(f)
        envs = job_template["spec"]["containers"][0]["env"]
        for i, env in enumerate(envs):
            if env["name"] == "LOCUST_URL":
                envs[i]["value"] = f"http://{self.locust_url}"
                break

        try:
            response = self.core_v1_api.create_namespaced_pod(
                namespace=self.namespace,
                body=job_template,
            )
            print("Waiting for locust-fetcher pod to be created...")
            while True:
                pod = self.core_v1_api.read_namespaced_pod_status(
                    name="locust-fetcher",
                    namespace=self.namespace,
                )
                conditions = pod.status.conditions or []
                ready = any(cond.type == "Ready" and cond.status == "True" for cond in conditions)
                if ready:
                    break
                time.sleep(5)
            print(f"Pod locust-fetcher created.")
        except client.exceptions.ApiException as e:
            print(f"Error creating pod: {e}")
            return

    def _parse_log(self, log_lines: list[dict]) -> WorkloadEntry:
        if "Running Locust on round #" not in log_lines[0]["content"]:
            raise ValueError("Log does not contain expected start pattern.")
        if len(log_lines) != 2:
            raise ValueError("Log does not contain exactly two lines for parsing.")

        parsed_log = json.loads(log_lines[1]["content"])

        start_time = log_lines[1]["time"][0:26] + "Z"
        start_time = datetime.strptime(start_time, "%Y-%m-%dT%H:%M:%S.%fZ").timestamp()

        has_error = False

        for items in parsed_log.get("errors", []):
            if items.get("occurrences", 0) > 0:
                has_error = True
                break

        num_requests = 0

        for items in parsed_log.get("stats", []):
            if items.get("safe_name", "") == "Aggregated":
                num_requests = items.get("num_requests", 0)
                break

        return WorkloadEntry(
            time=start_time,
            number=num_requests,
            log=log_lines[1]["content"],
            ok=not has_error,
        )

    def retrievelog(self, start_time: float | None = None) -> list[WorkloadEntry]:
        pods = self.core_v1_api.list_namespaced_pod(self.namespace, label_selector=f"app=locust-fetcher")

        if len(pods.items) == 0:
            raise Exception(f"No load-generator found in namespace {self.namespace}")

        kwargs = {
            "timestamps": True,
        }
        if start_time is not None:
            resp = stream.stream(
                self.core_v1_api.connect_get_namespaced_pod_exec,
                name=pods.items[0].metadata.name,
                namespace=self.namespace,
                command=["date", "-Ins"],
                stderr=True,
                stdin=False,
                stdout=True,
                tty=False,
            )

            shorter = resp.strip()[:26]
            pod_current_time = datetime.strptime(shorter, "%Y-%m-%dT%H:%M:%S,%f").timestamp()
            # Use the difference between pod's current time and requested start_time
            kwargs["since_seconds"] = math.ceil(pod_current_time - start_time) + STREAM_WORKLOAD_EPS

        try:
            logs = self.core_v1_api.read_namespaced_pod_log(pods.items[0].metadata.name, self.namespace, **kwargs)
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

        grouped_logs = []

        last_end = 0
        for i, log in enumerate(self.log_pool):
            if "Running Locust on round #" in log["content"]:
                try:
                    grouped_logs.append(self._parse_log(self.log_pool[last_end:i]))
                except Exception as e:
                    # Skip initialization logs and json parsing errors
                    pass
                last_end = i

        self.log_pool = self.log_pool[last_end:]

        return grouped_logs

    def start(self):
        logger.info("Start Workload with Locust")
        logger.info("AstronomyShop has a built-in load generator.")
        logger.info("Creating locust-fetcher pod...")
        self.create_fetcher()
        logger.debug("Workload started")

    def stop(self):
        logger.info("Stop Workload with Locust")
        logger.info("AstronomyShop's built-in load generator is automatically managed.")
        logger.info("Removing locust-fetcher pod if it exists...")
        self.remove_fetcher()
        logger.debug("Workload stopped")

    def change_users(self, number: int, namespace: str):
        increase_user_cmd = f"kubectl set env deployment/load-generator LOCUST_USERS={number} -n {namespace}"
        self.kubectl.exec_command(increase_user_cmd)

    def change_spawn_rate(self, rate: int, namespace: str):
        increase_spawn_rate_cmd = f"kubectl set env deployment/load-generator LOCUST_SPAWN_RATE={rate} -n {namespace}"
        self.kubectl.exec_command(increase_spawn_rate_cmd)

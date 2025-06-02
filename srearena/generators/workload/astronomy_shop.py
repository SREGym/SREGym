import math
from datetime import datetime

from kubernetes import client, config, stream

from srearena.generators.workload.stream import STREAM_WORKLOAD_EPS, StreamWorkloadManager, WorkloadEntry


class AstronomyShopWorkloadManager(StreamWorkloadManager):
    def __init__(self, deployment_name: str):
        super().__init__()

        self.deployment_name = deployment_name

        self.log_pool = []
        self.last_log_line_time = None

    def retrievelog(self, start_time: float | None = None) -> list[WorkloadEntry]:
        namespace = "astronomy-shop"

        pods = self.core_v1_api.list_namespaced_pod(namespace, label_selector=f"app.kubernetes.io/name=load-generator")

        if len(pods.items) == 0:
            raise Exception(f"No load-generator found in namespace {namespace}")

        kwargs = {
            "timestamps": True,
        }
        if start_time is not None:
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

        for i, log in enumerate(self.log_pool):
            start_time = log["time"][0:26] + "Z"
            start_time = datetime.strptime(start_time, "%Y-%m-%dT%H:%M:%S.%fZ").timestamp()
            grouped_logs.append(
                WorkloadEntry(
                    time=start_time, number=1, log=log["content"], ok=not "Failed" in log["content"]  # not this way
                )
            )
            if (i > 0 and "Requests/sec:" in self.log_pool[i - 1]["content"]) and "Transfer/sec:" in log["content"]:
                result = self._parse_log(self.log_pool[last_end : i + 1])
                grouped_logs.append(result)
                last_end = i + 1

        self.log_pool = []

        return grouped_logs
        return [WorkloadEntry(time=0.0, number=1, log="Sample log entry", ok=True)]

    def start(self):
        print("== Start Workload ==")
        print("AstronomyShop has a built-in load generator.")

    def stop(self):
        print("== Stop Workload ==")
        print("AstronomyShop's built-in load generator is automatically managed.")

"""Mattermost behind a bounded-worker gateway, with capacity automation on CPU.

The gateway's CPU falls when it saturates, so automation keyed on CPU removes
capacity from a service that is already failing, and reverts a manual scale-up.
Control state is on a persistent volume: the condition survives pod restarts.
"""

import json
import time
from pathlib import Path

from sregym.service.apps.mattermost import Mattermost

CONTROL_VOLUME = "gateway-control"
#: The service's documented capacity floor. Below this a single rollout or node
#: loss takes chat down, which is why the floor exists.
CAPACITY_FLOOR = 3
#: Workers per gateway pod. Small enough that a slow upstream saturates it.
GATEWAY_WORKERS = 8
SCALER_INTERVAL = 15
DEFAULT_SCALER_POLICY = {
    "enabled": True,
    "metric": "cpu",
    "scale_in_below": 25,
    "scale_out_above": 60,
    "min": 1,
    "max": 6,
}

#: A service reference, not an incident briefing. The capacity floor is a
#: documented SLO an on-call engineer would have; everything else about this
#: incident -- which signal misleads, what the automation does, where the
#: latency comes from -- is discoverable and is the task. Diagnosis is never
#: disclosed.
CASCADE_GUIDE = (
    """Chat gateway service reference

All customer traffic reaches Mattermost through `chat-gateway`, which serves each
request from a bounded pool of worker threads and exposes /metrics and /health.

`capacity-scaler` adjusts the gateway's replica count on a schedule. Its policy
and its decision log live in the gateway's control volume, mounted at /control in
both pods.

Documented service requirement: the gateway's capacity floor is """
    + str(CAPACITY_FLOOR)
    + """
replicas. Below that, a single rollout or node loss takes chat down.
"""
)


class MattermostCascade(Mattermost):
    data_volumes = (*Mattermost.data_volumes, CONTROL_VOLUME)
    #: Deliberately NOT in auxiliary_deployments: the shared SaaS oracle requires
    #: exactly one replica of each, and the gateway is meant to run several while
    #: deleting the scaler is a legitimate repair.
    cascade_deployments = ("chat-gateway", "capacity-scaler")

    capacity_floor = CAPACITY_FLOOR
    gateway_workers = GATEWAY_WORKERS
    scaler_interval = SCALER_INTERVAL

    def get_app_json(self):
        result = super().get_app_json()
        result["Desc"] += " Customer traffic reaches chat through chat-gateway; see /control/README.txt."
        return result

    def runtime_source(self, name):
        return Path(__file__).with_name("incident_runtime").joinpath(name).read_text()

    def scaler_rbac(self):
        """Least privilege: read gateway pods, read and write only its scale."""
        account = {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "capacity-scaler"}}
        role = {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": {"name": "capacity-scaler"},
            "rules": [
                {"apiGroups": [""], "resources": ["pods"], "verbs": ["list", "get"]},
                {
                    "apiGroups": ["apps"],
                    "resources": ["deployments/scale"],
                    "resourceNames": ["chat-gateway"],
                    "verbs": ["get", "patch", "update"],
                },
            ],
        }
        binding = {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": {"name": "capacity-scaler"},
            "subjects": [{"kind": "ServiceAccount", "name": "capacity-scaler"}],
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "capacity-scaler"},
        }
        return [account, role, binding]

    def application_documents(self):
        documents = super().application_documents()
        control = [{"name": "control", "persistentVolumeClaim": {"claimName": CONTROL_VOLUME}}]

        gateway = self.deployment(
            "chat-gateway",
            {
                "name": "gateway",
                "image": "python:3.12.13-alpine3.23",
                "command": ["python", "-u", "-c", self.runtime_source("chat_gateway.py")],
                "env": [
                    {"name": "GATEWAY_UPSTREAM", "value": f"http://{self.slug}:{self.frontend_port}"},
                    {"name": "GATEWAY_WORKERS", "value": str(self.gateway_workers)},
                ],
                "volumeMounts": [{"name": "control", "mountPath": "/control"}],
                "readinessProbe": {"httpGet": {"path": "/health", "port": 8080}, "periodSeconds": 5},
                "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}},
            },
            control,
        )
        # Several replicas, unlike every other deployment in these prototypes.
        gateway["spec"]["replicas"] = self.capacity_floor
        # RollingUpdate so a scale change does not take the whole gateway down.
        gateway["spec"]["strategy"] = {"type": "RollingUpdate"}

        scaler = self.deployment(
            "capacity-scaler",
            {
                "name": "scaler",
                "image": "python:3.12.13-alpine3.23",
                "command": ["python", "-u", "-c", self.runtime_source("capacity_scaler.py")],
                "env": [
                    {"name": "NAMESPACE", "value": self.namespace},
                    {"name": "SCALER_TARGET", "value": "chat-gateway"},
                    {"name": "SCALER_INTERVAL", "value": str(self.scaler_interval)},
                ],
                "volumeMounts": [{"name": "control", "mountPath": "/control"}],
                "readinessProbe": {"httpGet": {"path": "/health", "port": 8080}, "periodSeconds": 5},
                "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}},
            },
            control,
        )
        scaler["spec"]["template"]["spec"]["serviceAccountName"] = "capacity-scaler"
        # This one pod needs an API token; everything else here keeps none.
        scaler["spec"]["template"]["spec"]["automountServiceAccountToken"] = True

        traffic = self.deployment(
            "chat-traffic",
            {
                "name": "traffic",
                "image": "python:3.12.13-alpine3.23",
                "command": ["python", "-u", "-c", self.traffic_source()],
                "resources": {"requests": {"cpu": "50m", "memory": "32Mi"}, "limits": {"memory": "128Mi"}},
            },
        )

        return [
            *documents,
            *self.scaler_rbac(),
            self.service("chat-gateway", 8080),
            self.service("capacity-scaler", 8080),
            gateway,
            scaler,
            traffic,
        ]

    def traffic_source(self):
        """Concurrent customer traffic, always on.

        A sequential probe never has more than one request in flight, so it could
        not saturate a worker pool however slow the upstream became -- and CPU
        utilization is only a meaningful signal while the gateway is serving.
        """
        return (
            "import threading,time,urllib.request\n"
            "def worker():\n"
            " while True:\n"
            "  try: urllib.request.urlopen('http://chat-gateway:8080/api/v4/system/ping',timeout=30).read()\n"
            "  except Exception: pass\n"
            "  time.sleep(0.05)\n"
            f"for _ in range({self.gateway_workers * self.capacity_floor}):\n"
            " threading.Thread(target=worker,daemon=True).start()\n"
            "while True: time.sleep(60)\n"
        )

    def deploy(self):
        super().deploy()
        for name in (*self.cascade_deployments, "chat-traffic"):
            self.command("rollout", "status", f"deployment/{name}", "--timeout=300s", timeout=330)
        self.write_control("upstream_delay_ms", "0\n")
        self.write_control("README.txt", CASCADE_GUIDE)
        self.wait_for_gateway()
        if not self.command("get", "configmap", "capacity-calibrated", "--ignore-not-found", "-o", "name").strip():
            self.calibrate_scaler()
            self.apply(
                [
                    {
                        "apiVersion": "v1",
                        "kind": "ConfigMap",
                        "metadata": {"name": "capacity-calibrated"},
                        "data": {"policy": self.read_control("scaler.json")},
                    }
                ]
            )

    def healthy_cpu(self, samples=3, pause=6):
        """Average gateway CPU under normal traffic, measured not assumed."""
        readings = []
        for index in range(samples):
            if index:
                time.sleep(pause)
            values = [s["cpu_percent"] for s in self.gateway_metrics() if "cpu_percent" in s]
            if values:
                readings.append(sum(values) / len(values))
        if not readings:
            raise RuntimeError("Could not measure gateway CPU utilization")
        return round(sum(readings) / len(readings), 2)

    def calibrate_scaler(self):
        """Set the capacity policy's thresholds from the observed healthy load.

        Absolute thresholds would depend on how fast this host happens to be. A
        policy calibrated against real healthy CPU is both what an operator would
        do and stable across machines -- while still reading the incident
        backwards, because saturation drives CPU far below the healthy band.
        """
        healthy = self.healthy_cpu()
        # Enabled explicitly here: until this point the scaler has no policy file
        # and deliberately does nothing, so it cannot resize the gateway against
        # an uncalibrated threshold while the baseline is still being measured.
        policy = self.scaler_policy(
            enabled=True,
            scale_in_below=max(3, round(healthy * 0.5)),
            scale_out_above=max(round(healthy * 0.5) + 20, round(healthy * 1.8)),
            calibrated_healthy_cpu_percent=healthy,
        )
        self.write_control(
            "capacity-policy.txt",
            "Capacity automation calibrated against observed healthy load.\n"
            f"healthy gateway CPU: {healthy}%\n"
            f"scale in below: {policy['scale_in_below']}%\n"
            f"scale out above: {policy['scale_out_above']}%\n"
            f"replica floor enforced by policy: {policy['min']}\n"
            f"service capacity floor: {self.capacity_floor}\n",
        )
        return policy

    def write_control(self, name, content):
        """Write through the gateway pod, which owns the control volume."""
        self.command(
            "exec",
            "-i",
            "deployment/chat-gateway",
            "--",
            "sh",
            "-c",
            'cat > "/control/$1"',
            "write-control",
            name,
            input_text=content,
        )

    def read_control(self, name):
        return self.command("exec", "deployment/chat-gateway", "--", "cat", f"/control/{name}")

    def set_upstream_delay(self, milliseconds):
        self.write_control("upstream_delay_ms", f"{int(milliseconds)}\n")

    def scaler_policy(self, **changes):
        """Write a policy file, defaulting to the shipped CPU-keyed policy."""
        policy = {**DEFAULT_SCALER_POLICY, **changes}
        self.write_control("scaler.json", json.dumps(policy, indent=2) + "\n")
        return policy

    def gateway_replicas(self):
        scale = json.loads(self.command("get", "deployment", "chat-gateway", "-o", "json"))
        return scale["spec"].get("replicas", 0), scale.get("status", {}).get("readyReplicas", 0) or 0

    def gateway_metrics(self):
        """Read every gateway pod directly; the Service reaches only one."""
        pods = json.loads(self.command("get", "pods", "-l", "app=chat-gateway", "-o", "json"))["items"]
        samples = []
        for pod in pods:
            if pod["metadata"].get("deletionTimestamp") or pod.get("status", {}).get("phase") != "Running":
                continue
            name = pod["metadata"]["name"]
            try:
                samples.append(
                    json.loads(
                        self.command(
                            "exec",
                            name,
                            "--",
                            "python",
                            "-c",
                            "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8080/metrics',"
                            "timeout=5).read().decode())",
                            timeout=40,
                        )
                    )
                )
            except Exception:
                continue
        return samples

    def scaler_decisions(self):
        """The automation's own decision log, as an operator would read it."""
        try:
            raw = self.read_control("scaler-decisions.jsonl")
        except Exception:
            return []
        return [json.loads(line) for line in raw.splitlines() if line.strip()]

    def probe_through_gateway(self, attempts=12):
        """Measure what a customer sees: shed requests and latency, via the gateway."""
        script = (
            "import json,time,urllib.error,urllib.request\n"
            "shed=served=0;lat=[]\n"
            f"for _ in range({attempts}):\n"
            " s=time.monotonic()\n"
            " try:\n"
            "  urllib.request.urlopen('http://chat-gateway:8080/api/v4/system/ping',timeout=30).read()\n"
            "  served+=1\n"
            " except urllib.error.HTTPError as e:\n"
            "  shed+=1 if e.code==503 else 0\n"
            "  served+=0 if e.code==503 else 1\n"
            " except Exception:\n"
            "  shed+=1\n"
            " lat.append(time.monotonic()-s)\n"
            "lat.sort()\n"
            "print(json.dumps({'served':served,'shed':shed,"
            "'p50_ms':round(lat[len(lat)//2]*1000,1),'max_ms':round(lat[-1]*1000,1)}))\n"
        )
        return json.loads(
            self.command("exec", "-i", "application-client", "--", "python", "-", input_text=script, timeout=180)
        )

    def wait_for_gateway(self, timeout=180):
        deadline = time.monotonic() + timeout
        while True:
            try:
                if self.probe_through_gateway(attempts=3)["served"] == 3:
                    return
            except Exception:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("Chat gateway did not serve traffic")
            time.sleep(5)

    def start_workload(self):
        """A no-op: customer traffic is part of the environment and always on.

        Adding the inherited sequential health probe on top would only muddy the
        CPU signal the capacity policy is calibrated against.
        """
        return

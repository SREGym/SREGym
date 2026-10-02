"""A coordination cluster with dependent services and circular observability.

The incident's physics live in `incident_runtime/coordination_store.py`. This
module puts them on a cluster: a coordination service on a persistent volume,
customer-facing services that resolve backends through it, and a metrics
collector that discovers its scrape targets through it too -- so the aggregated
telemetry goes blind exactly when it is needed.
"""

import json
import time
from pathlib import Path

from sregym.service.apps.incident_runtime import coordination_store
from sregym.service.apps.mattermost import Mattermost

CONTROL_VOLUME = "coordination-control"
#: Customer-facing services that depend on coordination for backend lookup.
SESSION_SERVICES = ("session-service", "presence-service")
#: Recovery has a measured floor of ~370s with flawless play, so the agent needs
#: materially more than the 900s the earlier families used. The budget is part
#: of the task definition, not a runner detail.
RECOMMENDED_AGENT_TIMEOUT = 2700

TIER_SETTINGS = {
    # stability window, cache warming, admission step
    "single": (60, 120, 45),
    "replicated": (90, 180, 60),
}

#: The operator surface is a custom HTTP API: no amount of kubectl reveals that
#: `/v1/operator/compact` exists, so without this reference the incident is
#: unsolvable rather than hard. It is a tool reference and nothing more -- it
#: states what each verb does and which one is destructive, the way an API doc
#: would, and says nothing about the incident, which signals to trust, or what
#: order to do anything in. Diagnosis is the task and is never disclosed.
SERVICE_REFERENCE = """Coordinator service reference

`coordinator` is a three-member coordination service. `session-service` and
`presence-service` resolve their backends through it; `discovery-metrics`
aggregates service metrics. Coordinator state is on a persistent volume.

HTTP surface:

  GET  /status                    service status
  GET  /v1/internal/truth         full internal state, read-only, no side effects
  GET  /keys                      enumerate every key in the store
  GET  /ledger                    operations recorded on this cluster
  POST /v1/operator/shed              {"watch_subscriptions": N}
  POST /v1/operator/compact           {}
  POST /v1/operator/rebuild-scheduler {}
  POST /v1/operator/admit             {"fraction": 0.0-1.0}
  POST /v1/operator/force-reset       {"member": "..."}   DESTRUCTIVE: wipes a
                                      member's store; it cannot serve again.

Operator calls return an error body explaining any refusal. Each service also
exposes its own /metrics.

Documented service requirements: the coordinator must serve all admitted traffic,
all three members must remain usable, and customer requests must not be dropped.
"""


class CoordinationCluster(Mattermost):
    """Reuses the PostgreSQL SaaS lifecycle for its business-state checks.

    The coordination incident is about availability and recovery sequencing
    rather than stored records, but inheriting the SaaS application keeps the
    shared oracle's retained-data and durability guarantees in force, so an
    agent cannot "recover" by discarding business state.
    """

    data_volumes = (*Mattermost.data_volumes, CONTROL_VOLUME)
    auxiliary_deployments = ()
    #: Not auxiliary: these legitimately scale and one is legitimately deletable,
    #: so the shared oracle's exactly-one-replica rule must not apply to them.
    incident_deployments = ("coordinator", "discovery-metrics", *SESSION_SERVICES)
    coordination_members = 3

    @property
    def settings(self):
        return TIER_SETTINGS[self.scale_tier]

    @property
    def recovery_floor_seconds(self):
        """The fastest a flawless recovery can possibly complete."""
        stability, warming, step = self.settings
        return stability + warming + 4 * step

    def get_app_json(self):
        result = super().get_app_json()
        # Describes the application, not the incident: the agent is told what the
        # system is and where its API reference lives, and nothing else.
        result["Desc"] += (
            " Customer sessions resolve their backends through the `coordinator`"
            " service; its API reference is /control/README.txt in that pod."
        )
        return result

    def runtime_source(self, name):
        return Path(coordination_store.__file__).with_name(name).read_text()

    def application_documents(self):
        documents = super().application_documents()
        stability, warming, step = self.settings
        control = [{"name": "control", "persistentVolumeClaim": {"claimName": CONTROL_VOLUME}}]

        coordinator = self.deployment(
            "coordinator",
            {
                "name": "coordinator",
                "image": "python:3.12.13-alpine3.23",
                "command": ["python", "-u", "-c", self.runtime_source("coordination_store.py")],
                "env": [
                    {"name": "MEMBER_NAME", "value": "coordinator-0"},
                    {"name": "MEMBER_COUNT", "value": str(self.coordination_members)},
                    {"name": "STABILITY_SECONDS", "value": str(stability)},
                    {"name": "WARMING_SECONDS", "value": str(warming)},
                    {"name": "ADMISSION_STEP_SECONDS", "value": str(step)},
                ],
                "volumeMounts": [{"name": "control", "mountPath": "/control"}],
                # Liveness only, and deliberately not readiness: a coordinator
                # serving nothing still answers /health, so a green probe can
                # never be mistaken for recovery.
                "livenessProbe": {"httpGet": {"path": "/health", "port": 8080}, "periodSeconds": 10},
                "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}},
            },
            control,
        )

        sessions = [
            self.deployment(
                name,
                {
                    "name": "service",
                    "image": "python:3.12.13-alpine3.23",
                    "command": ["python", "-u", "-c", self.runtime_source("session_service.py")],
                    "env": [
                        {"name": "SERVICE_NAME", "value": name},
                        {"name": "COORDINATOR_URL", "value": "http://coordinator:8080"},
                    ],
                    "livenessProbe": {"httpGet": {"path": "/health", "port": 8080}, "periodSeconds": 10},
                    "resources": {"requests": {"cpu": "25m", "memory": "48Mi"}, "limits": {"memory": "128Mi"}},
                },
            )
            for name in SESSION_SERVICES
        ]

        metrics = self.deployment(
            "discovery-metrics",
            {
                "name": "collector",
                "image": "python:3.12.13-alpine3.23",
                "command": ["python", "-u", "-c", self.runtime_source("discovery_metrics.py")],
                "env": [
                    {"name": "COORDINATOR_URL", "value": "http://coordinator:8080"},
                    {"name": "SCRAPE_TARGETS", "value": ",".join(f"{s}:8080" for s in SESSION_SERVICES)},
                ],
                "livenessProbe": {"httpGet": {"path": "/health", "port": 8080}, "periodSeconds": 10},
                "resources": {"requests": {"cpu": "25m", "memory": "48Mi"}, "limits": {"memory": "128Mi"}},
            },
        )

        services = [self.service(name, 8080) for name in ("coordinator", "discovery-metrics", *SESSION_SERVICES)]
        return [*documents, *services, coordinator, *sessions, metrics]

    def deploy(self):
        super().deploy()
        for name in self.incident_deployments:
            self.command("rollout", "status", f"deployment/{name}", "--timeout=300s", timeout=330)
        self.write_control("README.txt", SERVICE_REFERENCE)
        self.wait_for_coordinator()

    def write_control(self, name, content):
        self.command(
            "exec",
            "-i",
            "deployment/coordinator",
            "--",
            "sh",
            "-c",
            'cat > "/control/$1"',
            "write-control",
            name,
            input_text=content,
        )

    @staticmethod
    def request_script(path, payload=None):
        """Build the in-cluster request script.

        The body and path are embedded with ``repr`` rather than ``json.dumps``
        because this is Python source, not JSON: ``json.dumps(None)`` is the
        literal ``null``, which is a NameError once it lands in a script.
        Separated out so it can be compiled in a unit test without a cluster.
        """
        body = json.dumps(payload) if payload is not None else None
        return (
            "import urllib.request,urllib.error\n"
            f"body = {body!r}\n"
            f"url = {('http://coordinator:8080' + path)!r}\n"
            "req = urllib.request.Request(\n"
            "    url,\n"
            "    data=body.encode() if body is not None else None,\n"
            "    method='POST' if body is not None else 'GET',\n"
            "    headers={'Content-Type': 'application/json'} if body is not None else {},\n"
            ")\n"
            "try:\n"
            "    print(urllib.request.urlopen(req, timeout=60).read().decode())\n"
            "except urllib.error.HTTPError as exc:\n"
            "    print(exc.read().decode())\n"
        )

    def coordinator_request(self, path, payload=None, timeout=90):
        """Call the coordinator from inside the cluster, as an operator would."""
        return json.loads(
            self.command(
                "exec",
                "-i",
                "application-client",
                "--",
                "python",
                "-",
                input_text=self.request_script(path, payload),
                timeout=timeout,
            )
        )

    def induce_collapse(self):
        """Develop the incident on a cluster that deployed healthy.

        Goes through the coordinator so the change is serialised under its own
        lock rather than racing the ticker that persists state every few seconds.
        """
        return self.coordinator_request("/v1/internal/induce", {})

    def truth(self):
        """The unvarnished coordination state, as the grader and a careful agent read it."""
        return self.coordinator_request("/v1/internal/truth")

    def operate(self, action, **payload):
        return self.coordinator_request(f"/v1/operator/{action}", payload)

    def ledger(self):
        return self.coordinator_request("/ledger")["events"]

    def service_metrics(self, name):
        script = (
            "import json,urllib.request\n"
            f"print(urllib.request.urlopen('http://{name}:8080/metrics', timeout=20).read().decode())\n"
        )
        return json.loads(
            self.command("exec", "-i", "application-client", "--", "python", "-", input_text=script, timeout=60)
        )

    def customer_probe(self, attempts=20):
        """What a customer sees, measured through the dependent services."""
        script = (
            "import json,urllib.error,urllib.request\n"
            "ok=shed=failed=0\n"
            f"for i in range({attempts}):\n"
            f"  target = {list(SESSION_SERVICES)!r}[i % {len(SESSION_SERVICES)}]\n"
            "  try:\n"
            "    urllib.request.urlopen(f'http://{target}:8080/session', timeout=20).read(); ok+=1\n"
            "  except urllib.error.HTTPError as e:\n"
            "    shed += 1 if e.code == 429 else 0\n"
            "    failed += 0 if e.code == 429 else 1\n"
            "  except Exception:\n"
            "    failed += 1\n"
            "print(json.dumps({'served':ok,'not_admitted':shed,'failed':failed}))\n"
        )
        return json.loads(
            self.command("exec", "-i", "application-client", "--", "python", "-", input_text=script, timeout=180)
        )

    def wait_for_coordinator(self, timeout=180):
        deadline = time.monotonic() + timeout
        while True:
            try:
                return self.truth()
            except Exception:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(5)

    def reference_recovery(self):
        """The flawless operator path. Takes the floor, and never rushes a gate."""
        stability, warming, step = self.settings
        self.operate("shed", watch_subscriptions=coordination_store.WATCH_BUDGET)
        # Wait the stability window out without touching the gated endpoints: an
        # early compaction attempt would restart the very window it waits on.
        time.sleep(stability + 5)
        self.operate("compact")
        self.operate("rebuild-scheduler")
        time.sleep(warming + 5)
        for fraction in (0.25, 0.5, 0.75, 1.0):
            self.operate("admit", fraction=fraction)
            time.sleep(step + 5)
        return self.truth()

    def start_workload(self):
        """A no-op: admitted customer traffic is the operator's dial, not ours.

        Generating load here would drop requests the agent never chose to admit,
        and the dropped-request meter is part of the grade.
        """
        return

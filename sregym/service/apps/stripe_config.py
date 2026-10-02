"""Stripe behind a persistent, periodically regenerated bot-configuration edge."""

import json
from pathlib import Path

from sregym.service.apps.stripe_marathon import StripeMarathon

CATALOG_IMAGE = "clickhouse/clickhouse-server:25.8.12.129-alpine"
RUNTIME_IMAGE = "python:3.12.13-alpine3.23"
COUNTS = {"single": 6, "replicated": 24}


class StripeConfig(StripeMarathon):
    data_volumes = (*StripeMarathon.data_volumes, "feature-control", "feature-catalog")
    auxiliary_deployments = (*StripeMarathon.auxiliary_deployments, "stripe-origin", "feature-catalog")

    def get_app_json(self):
        result = super().get_app_json()
        result["Desc"] += (
            " The payments edge publishes periodic ClickHouse-derived bot configuration"
            " to a persistent volume mounted at /control."
        )
        return result

    def application_documents(self):
        documents = super().application_documents()
        runtime = Path(__file__).with_name("incident_runtime")
        scripts = {p.name: p.read_text() for p in runtime.glob("*.py")}
        volumes = [{"name": "incident", "configMap": {"name": "incident-runtime"}}]
        mount = {"name": "incident", "mountPath": "/incident", "readOnly": True}
        for document in documents:
            if document["kind"] != "Deployment":
                continue
            spec = document["spec"]["template"]["spec"]
            if document["metadata"]["name"] == self.slug:
                document["metadata"]["name"] = "stripe-origin"
                document["spec"]["selector"]["matchLabels"]["app"] = "stripe-origin"
                document["spec"]["template"]["metadata"]["labels"]["app"] = "stripe-origin"
            if document["metadata"]["name"] in ("stripe-origin", "stripe-worker"):
                spec["containers"][0]["env"].append({"name": "STRIPE_WEBHOOK_RETRY_SCHEDULE", "value": "1,2,4"})
            if document["metadata"]["name"] == "stripe-worker":
                spec["volumes"] += volumes
                spec["containers"][0]["volumeMounts"] = [mount]
        control = [{"name": "control", "persistentVolumeClaim": {"claimName": "feature-control"}}]
        common = {
            "image": RUNTIME_IMAGE,
            "env": [self.secret_env("CATALOG_PASSWORD", "password")],
            "volumeMounts": [mount, {"name": "control", "mountPath": "/control"}],
            "resources": {"requests": {"cpu": "25m", "memory": "32Mi"}, "limits": {"memory": "256Mi"}},
        }
        edge = self.deployment(
            self.slug,
            {
                **common,
                "name": "edge",
                "command": ["python", "-u", "/incident/feature_edge.py", "edge"],
                **self.probes("/ready"),
            },
            [*volumes, *control],
        )
        edge["spec"]["template"]["spec"]["containers"].append(
            {
                **common,
                "name": "producer",
                "command": ["python", "-u", "/incident/feature_edge.py", "producer"],
            }
        )
        catalog = {
            "name": "catalog",
            "image": CATALOG_IMAGE,
            "env": [
                {"name": "CLICKHOUSE_USER", "value": "operator"},
                self.secret_env("CLICKHOUSE_PASSWORD", "password"),
                {"name": "CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT", "value": "1"},
            ],
            "volumeMounts": [{"name": "catalog", "mountPath": "/var/lib/clickhouse"}],
            "readinessProbe": {"httpGet": {"path": "/ping", "port": 8123}, "initialDelaySeconds": 5},
            "resources": {"requests": {"cpu": "200m", "memory": "512Mi"}, "limits": {"memory": "2Gi"}},
        }
        return [
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "incident-runtime"}, "data": scripts},
            *documents,
            self.service("stripe-origin", 8000),
            self.service("feature-catalog", 8123),
            self.deployment(
                "feature-catalog",
                catalog,
                [{"name": "catalog", "persistentVolumeClaim": {"claimName": "feature-catalog"}}],
            ),
            edge,
        ]

    def run_client(self, mode, **arguments):
        if mode == "seed":
            arguments["webhook_url"] = "http://stripe-marathon:8000/hook"
        return super().run_client(mode, **arguments)

    def control(self, action, argument=None):
        args = ["python", "/incident/feature_edge.py", action]
        if argument is not None:
            args.append(argument if isinstance(argument, str) else json.dumps(argument))
        return self.command("exec", "deployment/stripe-marathon", "-c", "edge", "--", *args)

    def control_write(self, name, content):
        return self.command(
            "exec",
            "-i",
            "deployment/stripe-marathon",
            "-c",
            "edge",
            "--",
            "python",
            "-c",
            "import pathlib,sys; p=pathlib.Path('/control')/sys.argv[1]; t=p.with_suffix('.new'); t.write_text(sys.stdin.read()); t.replace(p)",
            name,
            input_text=content,
        )

    def configuration_safety(self):
        # Grading code comes from the harness, not the editable incident ConfigMap.
        source = Path(__file__).with_name("incident_runtime").joinpath("feature_edge.py").read_text()
        verifier = (
            f"import json\nscope={{'__name__': 'verifier'}}\nexec({source!r}, scope)\n"
            "print(json.dumps(scope['safety']()))\n"
        )
        return json.loads(
            self.command(
                "exec",
                "-i",
                "deployment/stripe-marathon",
                "-c",
                "edge",
                "--",
                "python",
                "-",
                input_text=verifier,
            )
        )

    def state(self, pod=None):
        return json.loads(self.sql("SELECT state FROM marathon_state WHERE id=1;", pod=pod))

    def receipts(self):
        source = "import json,urllib.request; print(urllib.request.urlopen('http://stripe-receiver:8080/receipts').read().decode())"
        return json.loads(self.command("exec", "application-client", "--", "python", "-c", source))

    def requeue(self, event_ids=None):
        args = ["env", "PYTHONPATH=/app", "python", "/incident/requeue_webhooks.py"]
        if event_ids is not None:
            args.append(json.dumps(event_ids))
        return json.loads(self.command("exec", "deployment/stripe-worker", "--", *args))

    def set_worker(self, replicas):
        self.command("scale", "deployment/stripe-worker", f"--replicas={replicas}")
        if replicas:
            self.command("rollout", "status", "deployment/stripe-worker", "--timeout=180s", timeout=200)
        else:
            self.command("wait", "--for=delete", "pod", "-l", "app=stripe-worker", "--timeout=180s", timeout=200)

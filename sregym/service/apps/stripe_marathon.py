"""SWE-Marathon Stripe reference, durable API state and a separate retry worker."""

import hashlib
from pathlib import Path

from sregym.service.apps.postgres_saas import PostgresSaaS


def image_tag():
    root = Path(__file__).resolve().parents[3]
    paths = [
        *sorted((root / "docker/stripe-marathon").glob("*")),
        root / "sregym/service/apps/fixtures/swe-marathon-stripe/upstream.json",
        root / "sregym/service/apps/fixtures/swe-marathon-stripe/NOTICE",
    ]
    digest = hashlib.sha256()
    for path in paths:
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes())
    return "sregym-stripe-marathon:" + digest.hexdigest()[:16]


STRIPE_IMAGE = image_tag()


class StripeMarathon(PostgresSaaS):
    slug = "stripe-marathon"
    frontend_port = 8000
    health_path = "/v1/health"
    data_volumes = ("stripe-receipts",)
    auxiliary_deployments = ("stripe-worker", "stripe-receiver")

    def application_documents(self):
        env = [
            self.secret_env("DATABASE_PASSWORD", "password", "application-database"),
            {
                "name": "DATABASE_URL",
                "value": "postgresql://stripe_marathon:$(DATABASE_PASSWORD)@stripe-marathon-db-rw:5432/stripe_marathon?sslmode=require&connect_timeout=5",
            },
            self.secret_env("STRIPE_SK", "token"),
            {"name": "STRIPE_IDEMPOTENCY_TTL", "value": "86400"},
        ]
        resources = {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"memory": "512Mi"}}
        api = {
            "name": "api",
            "image": STRIPE_IMAGE,
            "imagePullPolicy": "IfNotPresent",
            "env": env,
            "resources": resources,
            **self.probes(),
        }
        worker = {
            "name": "worker",
            "image": STRIPE_IMAGE,
            "imagePullPolicy": "IfNotPresent",
            "env": env,
            "command": ["python", "backend.py", "worker"],
            "resources": resources,
        }
        receiver = {
            "name": "receiver",
            "image": STRIPE_IMAGE,
            "imagePullPolicy": "IfNotPresent",
            "command": ["python", "receiver.py"],
            "volumeMounts": [{"name": "data", "mountPath": "/data"}],
            "resources": resources,
            "readinessProbe": {"httpGet": {"path": "/receipts", "port": 8080}},
        }
        return [
            self.deployment(self.slug, api),
            self.deployment("stripe-worker", worker),
            self.service("stripe-receiver", 8080),
            self.deployment(
                "stripe-receiver",
                receiver,
                [{"name": "data", "persistentVolumeClaim": {"claimName": "stripe-receipts"}}],
            ),
        ]

    def record_query(self, token):
        return (
            "SELECT count(*) FROM marathon_state WHERE id=1 AND EXISTS (SELECT 1 FROM jsonb_each(state->'customers') c WHERE c.value->'metadata'->>'probe' = '"
            + token
            + "');"
        )

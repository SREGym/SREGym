"""Deployment for the airport meal partner services."""

from hashlib import sha256
from pathlib import Path
from string import Template

import yaml

from sregym.cleanup import cleanup_drain_timeout
from sregym.paths import BASE_DIR, BASE_PARENT_DIR
from sregym.service.apps.base import Application
from sregym.service.apps.components.kubernetes import KubernetesCommands
from sregym.service.apps.persistent import cleanup_persistent_namespace
from sregym.service.kubectl import KubeCtl


class AirportMealClearingApplication(KubernetesCommands, Application):
    metadata_file = Path(__file__).with_name("metadata.json")
    manifest_file = Path(__file__).with_name("partner.yaml")
    partner_dir = BASE_DIR / "conductor/problems/assets/airport_meal_clearing/partners"
    operations_file = BASE_PARENT_DIR / "docs/airport-meal-clearing.md"
    partners = {
        "north": "carrier",
        "south": "carrier",
        "gate-a": "cafe",
        "gate-b": "cafe",
        "gate-c": "cafe",
        "gate-d": "cafe",
        "clearing": "clearing",
    }
    frontend_service = "clearing"
    frontend_port = 8080

    def __init__(self):
        super().__init__(self.metadata_file)
        self.load_app_json()
        self.app_name = self.name
        self.description = self.get_app_json()["Desc"]
        self.helm_deploy = False
        self.kubectl = KubeCtl()

    def deploy(self):
        runtime = {
            "app.py": (self.partner_dir / "app.py").read_text(),
            "INTERFACES.md": (self.partner_dir / "INTERFACES.md").read_text(),
            "flink-session.yaml": (self.partner_dir.parent / "stream/flink.yaml").read_text(),
            "OPERATIONS.md": self.operations_file.read_text(),
        }
        source_hash = sha256(runtime["app.py"].encode()).hexdigest()
        manifest = Template(self.manifest_file.read_text())
        documents = [
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "meal-partner-runtime"},
                "data": runtime,
            }
        ]
        for site, role in self.partners.items():
            documents.extend(yaml.safe_load_all(manifest.substitute(site=site, role=role, source_hash=source_hash)))

        self.command("get", "storageclass", "openebs-hostpath")
        self.create_namespace()
        self.apply(documents)
        for site in self.partners:
            self.command("rollout", "status", f"deployment/{site}", "--timeout=180s", timeout=190)

    def cleanup(self):
        cleanup_persistent_namespace(self, timeout=cleanup_drain_timeout())

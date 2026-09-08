"""Render actual chart templates: unknown values paths otherwise fail silently."""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "SREGym-applications/astronomy-shop/charts/opentelemetry-demo"
VALUES = ROOT / "sregym/service/apps/values"


@pytest.mark.integration
@pytest.mark.parametrize("profile", ["full", "svelte"])
def test_runtime_fixes_reach_rendered_containers(profile):
    if not shutil.which("helm") or not (CHART / "charts").exists():
        pytest.skip("Requires Helm and initialized chart dependencies")
    command = [
        "helm",
        "template",
        "astronomy-shop",
        str(CHART),
        "--set",
        "prometheus.enabled=false",
        "-f",
        str(VALUES / "astronomy-shop-fixes.yaml"),
    ]
    if profile == "svelte":
        command += ["-f", str(VALUES / "astronomy-shop-svelte.yaml")]
    rendered = subprocess.run(command, capture_output=True, text=True, check=True, timeout=60)
    containers = {}
    for obj in yaml.safe_load_all(rendered.stdout):
        if obj and obj["kind"] == "Deployment":
            containers.update({c["name"]: c for c in obj["spec"]["template"]["spec"]["containers"]})
    accounting = containers["accounting"]
    assert accounting["image"].endswith("@sha256:51e6720438c13d0a4fefd129b01e2422b176fdb18a9ea71ef08fd84e717e82f4")
    for name, heap in [("ad", "200m"), ("fraud-detection", "180m")]:
        c = containers[name]
        env = [e["value"] for e in c["env"] if e["name"] == "JAVA_TOOL_OPTIONS"]
        assert len(env) == 1
        assert "-javaagent:/usr/src/app/opentelemetry-javaagent.jar" in env[0]
        assert f"-Xmx{heap}" in env[0]
        assert c["resources"]["limits"]["memory"] == "300Mi"

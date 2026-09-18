"""Guard the external platform and dynamically launched action images together."""

import json
import re
import runpy
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
FLIGHT = ROOT / "SREGym-applications/flight-ticket"
PROFILE = FLIGHT / "openwhisk"
CHECKER = runpy.run_path(str(ROOT / "docker/check_image_platforms.py"))
RUNTIME = runpy.run_path(str(ROOT / "docker/openwhisk/test_runtime.py"))


def test_openwhisk_profile_uses_all_nine_locked_releases():
    lock = json.loads((ROOT / "docker/images.lock.json").read_text())
    expected = {lock[f"openwhisk-{component}"] for component in RUNTIME["PROGRAMS"]}
    assert len(expected) == 9
    assert all(re.fullmatch(r"ghcr\.io/sregym/openwhisk:[^@]+@sha256:[a-f0-9]{64}", ref) for ref in expected)
    values = yaml.safe_load((PROFILE / "values.yaml").read_text())
    sections = [values[name] for name in ("controller", "invoker", "utility", "zookeeper", "apigw")]
    sections.extend(values["providers"].values())
    images = {f"{section['imageName']}:{section['imageTag']}" for section in sections}
    images.update(CHECKER["runtime_images"](json.loads((PROFILE / "runtimes.json").read_text())))
    assert images == expected | {lock["flight-ticket-python-runtime"]}
    assert values["invoker"]["containerFactory"]["impl"] == "kubernetes"
    assert values["whisk"]["runtimes"] == "sregym-runtimes.json"


def test_runtime_defaults_are_explicit_and_unambiguous():
    catalog = json.loads((PROFILE / "runtimes.json").read_text())
    assert set(catalog["runtimes"]) == {"nodejs", "python"}
    for runtimes in catalog["runtimes"].values():
        assert len([runtime for runtime in runtimes if runtime.get("default")]) == 1
    assert {runtime["kind"] for group in catalog["runtimes"].values() for runtime in group} == {"nodejs:14", "python:3"}


def test_catalog_adaptation_preserves_every_non_swift_action(tmp_path):
    # Run the actual embedded adaptation, not a duplicate implementation.
    patch = (PROFILE / "chart.patch").read_text()
    added = "\n".join(line[1:] for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
    script = added.split("python - <<'PY'\n", 1)[1].split("\nPY", 1)[0]
    samples = tmp_path / "samples"
    samples.mkdir()
    actions = {name: {"function": f"{name}.swift"} for name in ("cat", "head", "invoke")}
    kept = {"helloWorld": {"function": "hello.js"}, "wordCount": {"function": "words.js"}}
    manifest = {"project": {"packages": {"samples": {"public": True, "actions": actions | kept}}}}
    path = samples / "manifest.yaml"
    path.write_text(yaml.safe_dump(manifest))
    subprocess.run([sys.executable, "-c", script], cwd=tmp_path, check=True)
    result = yaml.safe_load(path.read_text())["project"]["packages"]["samples"]
    assert result == {"public": True, "actions": kept}
    # Fail closed if the pinned upstream catalog changes unexpectedly.
    assert subprocess.run([sys.executable, "-c", script], cwd=tmp_path, capture_output=True).returncode != 0


def test_setup_pins_chart_and_applies_profile_without_editing_workflows():
    script = (FLIGHT / "setup_openwhisk.sh").read_text()
    assert re.search(r"^chart_revision=[a-f0-9]{40}$", script, re.MULTILINE)
    assert 'git -C "$chart_dir" apply "$script_dir/openwhisk/chart.patch"' in script
    assert '"$script_dir/openwhisk/runtimes.json"' in script
    assert '"$script_dir/openwhisk/action-data-policy.yaml"' in script
    assert '--values "$script_dir/openwhisk/values.yaml" "$@"' in script
    subprocess.run(["bash", "-n", str(FLIGHT / "setup_openwhisk.sh")], check=True)


def test_action_data_access_is_limited_to_each_store_and_current_release(tmp_path):
    (tmp_path / "Chart.yaml").write_text("apiVersion: v2\nname: policy-test\nversion: 0.1.0\n")
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "policy.yaml").write_text((PROFILE / "action-data-policy.yaml").read_text())
    output = subprocess.check_output(["helm", "template", "another-release", str(tmp_path)], text=True)
    policies = list(yaml.safe_load_all(output))
    assert len(policies) == 2
    for policy, (service, port) in zip(policies, (("couchdb", 5984), ("redis", 6379)), strict=True):
        assert policy["spec"] == {
            "podSelector": {"matchLabels": {"name": f"another-release-{service}"}},
            "policyTypes": ["Ingress"],
            "ingress": [
                {
                    "from": [
                        {"podSelector": {"matchLabels": {"release": "another-release", "user-action-pod": "true"}}}
                    ],
                    "ports": [{"protocol": "TCP", "port": port}],
                }
            ],
        }

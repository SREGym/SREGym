import json
import runpy
import subprocess
from pathlib import Path

import pytest

_checker = runpy.run_path(str(Path(__file__).resolve().parents[2] / "docker/check_image_platforms.py"))
REQUIRED_PLATFORMS = _checker["REQUIRED_PLATFORMS"]
check_image = _checker["check_image"]
container_images = _checker["container_images"]
index_platforms = _checker["index_platforms"]
runtime_images = _checker["runtime_images"]


def test_runtime_images_cover_every_language_and_blackbox():
    manifest = {
        "runtimes": {
            "nodejs": [{"image": {"prefix": "ghcr.io/sregym", "name": "openwhisk", "tag": "node@sha256:abc"}}],
            "python": [{"image": {"prefix": "", "name": "python", "tag": "3"}}],
        },
        "blackboxes": [{"registry": "example.org/", "prefix": "", "name": "custom", "tag": "v1"}],
    }
    expected = {"ghcr.io/sregym/openwhisk:node@sha256:abc", "python:3", "example.org/custom:v1"}
    assert runtime_images(manifest) == expected
    pod = {
        "spec": {
            "containers": [
                {
                    "image": "controller:v1",
                    "env": [
                        {"name": "RUNTIMES_MANIFEST", "value": json.dumps(manifest)},
                    ],
                }
            ]
        }
    }
    assert container_images(pod) == expected | {"controller:v1"}


def test_invalid_embedded_runtime_manifest_fails_closed():
    with pytest.raises(ValueError):
        container_images({"name": "RUNTIMES_MANIFEST", "value": "not json"})


def test_extracts_all_container_types_without_treating_config_values_as_images():
    document = {
        "items": [
            {
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [{"image": "app:v1"}, {"image": "sidecar:v2"}],
                            "initContainers": [{"image": "init:v3"}],
                            "ephemeralContainers": [{"image": "debug:v4"}],
                        }
                    }
                }
            },
            {"kind": "ConfigMap", "data": {"image": "not-a-container"}},
            None,
        ]
    }
    assert container_images(document) == {"app:v1", "sidecar:v2", "init:v3", "debug:v4"}


def test_platform_index_ignores_attestations_and_keeps_arm_variants():
    index = {
        "manifests": [
            {"platform": {"os": "linux", "architecture": "amd64"}},
            {"platform": {"os": "linux", "architecture": "arm64", "variant": "v8"}},
            {"platform": {"os": "unknown", "architecture": "unknown"}},
        ]
    }
    assert index_platforms(index) == REQUIRED_PLATFORMS


@pytest.mark.parametrize("architectures,valid", [(["amd64", "arm64"], True), (["amd64"], False), ([], False)])
def test_check_requires_both_architectures(monkeypatch, architectures, valid):
    index = {"manifests": [{"platform": {"os": "linux", "architecture": arch}} for arch in architectures]}
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess([], 0, json.dumps(index))
    )
    image, error = check_image("example:v1")
    assert image == "example:v1"
    assert (error is None) == valid


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired("docker", 120), OSError("missing docker")])
def test_registry_errors_fail_closed(monkeypatch, failure):
    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(subprocess, "run", fail)
    assert "registry inspection failed" in check_image("example:v1")[1]

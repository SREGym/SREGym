import json
import runpy
import subprocess
from pathlib import Path

import pytest

_checker = runpy.run_path(str(Path(__file__).resolve().parents[2] / "docker/check_image_platforms.py"))
REQUIRED_PLATFORMS = _checker["REQUIRED_PLATFORMS"]
check_image = _checker["check_image"]
container_images = _checker["container_images"]
configuration_images = _checker["configuration_images"]
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


def test_controller_launched_images_are_included_without_an_initial_pod():
    document = {
        "env": [
            {"name": "JOB_CONTAINER_IMAGE", "value": "registry.example/provisioner:v2"},
            {"name": "OPENEBS_IO_HELPER_IMAGE", "value": "openebs/linux-utils:3.5.0"},
            {"name": "OTHER_CONFIG", "value": "not-an-image"},
        ]
    }
    assert container_images(document) == {"registry.example/provisioner:v2", "openebs/linux-utils:3.5.0"}


def test_operator_images_include_disabled_components_and_backup_helpers():
    values = {
        "operatorImage": "pingcap/tidb-operator:v1.6.0",
        "tidbBackupManagerImage": "pingcap/tidb-backup-manager:v1.6.0",
        "advancedStatefulset": {"create": False, "image": "pingcap/advanced-statefulset:v0.7.0"},
    }
    assert configuration_images(values) == {
        "pingcap/tidb-operator:v1.6.0",
        "pingcap/tidb-backup-manager:v1.6.0",
        "pingcap/advanced-statefulset:v0.7.0",
    }


def test_operator_custom_resources_inherit_or_override_versions():
    document = {
        "spec": {
            "version": "v8.1.0",
            "tidb": {"baseImage": "pingcap/tidb"},
            "pd": {"baseImage": "pingcap/pd", "version": "v8.2.0"},
        }
    }
    assert configuration_images(document) == {"pingcap/tidb:v8.1.0", "pingcap/pd:v8.2.0"}
    with pytest.raises(ValueError, match="No version"):
        configuration_images({"baseImage": "pingcap/tidb"})


def test_chart_repository_and_tag_are_resolved_before_registry_checks():
    assert configuration_images({"image": "docker.elastic.co/beats/filebeat", "imageTag": "8.7.1"}) == {
        "docker.elastic.co/beats/filebeat:8.7.1",
    }
    assert configuration_images({"image": "localhost:5000/app", "tag": "v2"}) == {"localhost:5000/app:v2"}


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

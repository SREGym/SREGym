import copy
import runpy
import shutil
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
PINNER = runpy.run_path(str(ROOT / "docker/train-ticket/pin_images.py"))
SOURCE = ROOT / "SREGym-applications/train-ticket/deploy-job/deployment"
TARGETS = (
    "percona",
    "xenon",
    "mysqld-exporter",
    "nacos",
    "mysqlclient",
    "rabbitmq",
    "alertsnitch-mysql",
)


def test_image_parts_keeps_the_index_digest_and_registry_port():
    assert PINNER["image_parts"]("registry.test:5000/mysql:v1@sha256:abc") == (
        "registry.test:5000/mysql",
        "v1@sha256:abc",
    )


def test_mutable_image_references_are_rejected():
    with pytest.raises(ValueError, match="digest-pinned"):
        PINNER["image_parts"]("example/mysql:latest")


def test_installer_image_updates_preserve_all_other_configuration(tmp_path):
    deployment = tmp_path / "deployment"
    shutil.copytree(SOURCE, deployment)
    originals = {p.relative_to(deployment): p.read_bytes() for p in deployment.rglob("*") if p.is_file()}
    images = {f"train-ticket-{name}": f"registry.test/{name}:test@sha256:abc" for name in TARGETS}
    PINNER["pin_images"](deployment, images)

    charts = Path("kubernetes-manifests/quickstart-k8s/charts")
    expected_changes = {
        charts / "mysql/values.yaml",
        charts / "nacos/values.yaml",
        charts / "rabbitmq/values.yaml",
        Path("kubernetes-manifests/prometheus/alertsnitch.yml"),
    }
    assert {p for p, data in originals.items() if (deployment / p).read_bytes() != data} == expected_changes

    for path in expected_changes:
        before = list(yaml.safe_load_all(originals[path]))
        expected = copy.deepcopy(before)
        values = expected[0]
        if path == charts / "mysql/values.yaml":
            for field, name in (("mysql", "percona"), ("xenon", "xenon"), ("metrics", "mysqld-exporter")):
                values[field]["image"] = f"registry.test/{name}"
                values[field]["tag"] = "test@sha256:abc"
        elif path == charts / "nacos/values.yaml":
            values["nacos"]["image"].update(repository="registry.test/nacos", tag="test@sha256:abc")
            values["initmysql"]["image"] = images["train-ticket-mysqlclient"]
        elif path == charts / "rabbitmq/values.yaml":
            values["rabbitmq"]["image"].update(repository="registry.test/rabbitmq", tag="test@sha256:abc")
        else:
            values["spec"]["template"]["spec"]["containers"][0]["image"] = images["train-ticket-alertsnitch-mysql"]
        assert list(yaml.safe_load_all((deployment / path).read_text())) == expected

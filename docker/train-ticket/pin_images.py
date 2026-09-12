"""Pin only the database/helper images in the existing Train Ticket installer.

The installer embeds its own charts and manifests. Updating the separate
application source manifests does not change what the deployment job runs.
This build-time transformation preserves every other chart value and service.
"""

import argparse
import json
from pathlib import Path

import yaml


def image_parts(reference: str) -> tuple[str, str]:
    name, separator, digest = reference.partition("@")
    if not separator or not digest.startswith("sha256:"):
        raise ValueError(f"Expected a digest-pinned image: {reference}")
    repository, tag = name.rsplit(":", 1)
    return repository, f"{tag}@{digest}"


def pin_images(deployment: Path, images: dict[str, str]) -> None:
    charts = deployment / "kubernetes-manifests/quickstart-k8s/charts"
    mysql_path = charts / "mysql/values.yaml"
    mysql = yaml.safe_load(mysql_path.read_text())
    for component, target in (
        ("mysql", "train-ticket-percona"),
        ("xenon", "train-ticket-xenon"),
        ("metrics", "train-ticket-mysqld-exporter"),
    ):
        mysql[component]["image"], mysql[component]["tag"] = image_parts(images[target])
    mysql_path.write_text(yaml.safe_dump(mysql, sort_keys=False))

    nacos_path = charts / "nacos/values.yaml"
    nacos = yaml.safe_load(nacos_path.read_text())
    nacos["nacos"]["image"]["repository"], nacos["nacos"]["image"]["tag"] = image_parts(images["train-ticket-nacos"])
    nacos["initmysql"]["image"] = images["train-ticket-mysqlclient"]
    nacos_path.write_text(yaml.safe_dump(nacos, sort_keys=False))

    rabbit_path = charts / "rabbitmq/values.yaml"
    rabbit = yaml.safe_load(rabbit_path.read_text())
    rabbit["rabbitmq"]["image"]["repository"], rabbit["rabbitmq"]["image"]["tag"] = image_parts(
        images["train-ticket-rabbitmq"]
    )
    rabbit_path.write_text(yaml.safe_dump(rabbit, sort_keys=False))

    alerts_path = deployment / "kubernetes-manifests/prometheus/alertsnitch.yml"
    documents = list(yaml.safe_load_all(alerts_path.read_text()))
    matches = 0
    for document in documents:
        if document.get("kind") == "Deployment" and document["metadata"]["name"] == "alertsnitch-mysql":
            containers = document["spec"]["template"]["spec"]["containers"]
            for container in containers:
                if container["name"] == "alertsnitch-mysql":
                    container["image"] = images["train-ticket-alertsnitch-mysql"]
                    matches += 1
    if matches != 1:
        raise ValueError(f"Expected one Alertsnitch MySQL container, found {matches}")
    alerts_path.write_text(yaml.safe_dump_all(documents, sort_keys=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("deployment", type=Path)
    parser.add_argument("image_lock", type=Path)
    args = parser.parse_args()
    pin_images(args.deployment, json.loads(args.image_lock.read_text()))

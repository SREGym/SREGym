"""Synchronize committed image references from images.lock.json; no registry access."""

import argparse
import ast
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
HOTEL_CONTAINERS = {
    f"hotel-reserv-{name}"
    for name in ("frontend", "geo", "profile", "rate", "recommendation", "reservation", "search", "user")
}
PYTHON_IMAGES = {
    "sregym/generators/images.py": {
        "HOTEL_GEO_MISCONFIG_IMAGE": "hotel-reservation-1",
        "HOTEL_CORRELATED_FAULT_IMAGE": "hotel-reservation-2",
        "STRESS_IMAGE": "stress",
    },
    "sregym/service/container_runner.py": {"DEFAULT_AGENT_IMAGE": "agent-base"},
    "sregym/service/apps/hotel_reservation.py": {"HOTEL_RESERVATION_APPLICATION_IMAGE": "hotel-reservation"},
}


def image_parts(image: str) -> tuple[str, str]:
    if not re.fullmatch(r"[^\s@]+:[^\s@]+@sha256:[a-f0-9]{64}", image):
        raise ValueError(f"Expected a tagged multiarch index reference: {image}")
    name, digest = image.split("@")
    repository, tag = name.rsplit(":", 1)
    return repository, f"{tag}@{digest}"


def yaml_references(source: str, updates: dict[tuple, str]) -> str:
    """Change selected scalars without reformatting YAML or removing comments.

    Paths start with the YAML document index, followed by mapping keys or list indexes.
    """
    documents = list(yaml.compose_all(source))
    edits = []
    for path, value in updates.items():
        node = documents[path[0]]
        for key in path[1:]:
            if isinstance(node, yaml.SequenceNode) and isinstance(key, int):
                node = node.value[key]
            elif isinstance(node, yaml.MappingNode):
                matches = [child for name, child in node.value if name.value == key]
                if len(matches) != 1:
                    raise ValueError(f"Expected one YAML field at {path}, found {len(matches)}")
                node = matches[0]
            else:
                raise ValueError(f"Invalid YAML path: {path}")
        if not isinstance(node, yaml.ScalarNode):
            raise ValueError(f"Expected an image scalar at {path}")
        if node.value != value:
            edits.append((node.start_mark.index, node.end_mark.index, json.dumps(value)))
    for start, end, value in sorted(edits, reverse=True):
        source = source[:start] + value + source[end:]
    return source


def python_references(source: str, updates: dict[str, str]) -> str:
    lines = source.splitlines(keepends=True)
    found = set()
    edits = []
    for node in ast.parse(source).body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        name = getattr(node.targets[0], "id", None)
        if name not in updates:
            continue
        found.add(name)
        if ast.literal_eval(node.value) != updates[name]:
            reference, digest = updates[name].split("@")
            value = f'{name} = (\n    "{reference}"\n    "@{digest}"\n)\n'
            edits.append((node.lineno - 1, node.end_lineno, value))
    if found != updates.keys():
        raise ValueError(f"Missing image constants: {updates.keys() - found}")
    for start, end, value in sorted(edits, reverse=True):
        lines[start:end] = [value]
    return "".join(lines)


def render_consumers(root: Path, images: dict[str, str]) -> dict[Path, str]:
    """Prepare every edit before writing, so a missing consumer fails without partial updates."""
    parts = {name: image_parts(image) for name, image in images.items()}
    outputs = {}

    def update_yaml(path, updates):
        file = root / path
        outputs[file] = yaml_references(file.read_text(), updates)

    def update_dockerfile(path, prefix, image):
        file = root / path
        source, count = re.subn(rf"(?m)^{re.escape(prefix)}[^\n]+$", prefix + image, file.read_text())
        if count != 1:
            raise ValueError(f"Expected one {prefix} in {path}, found {count}")
        outputs[file] = source

    for path, constants in PYTHON_IMAGES.items():
        file = root / path
        outputs[file] = python_references(file.read_text(), {name: images[key] for name, key in constants.items()})

    pod = (0, "spec", "template", "spec", "containers")
    update_yaml("kind/kind-config.yaml", {(0, "nodes", n, "image"): images["kind-node"] for n in range(4)})
    update_yaml("sregym/generators/workload/wrk-job-template.yaml", {(*pod, 0, "image"): images["wrk2"]})
    update_yaml(
        "sregym/resources/trainticket/locust-deployment.yaml", {(1, *pod[1:], 1, "image"): images["locust-exporter"]}
    )
    for file in sorted((root / "SREGym-applications/hotelReservation/kubernetes").rglob("*-deployment.yaml")):
        document = yaml.safe_load(file.read_text())
        updates = {
            (*pod, i, "image"): images["hotel-reservation"]
            for i, container in enumerate(document["spec"]["template"]["spec"]["containers"])
            if container["name"] in HOTEL_CONTAINERS
        }
        if updates:
            update_yaml(file, updates)

    values = "sregym/service/apps/values"
    update_yaml(
        f"{values}/fleetcast-images.yaml",
        {
            (0, "backend", "image", field): value
            for field, value in zip(("repository", "tag"), parts["fleetcast-backend"], strict=True)
        },
    )
    update_yaml(f"{values}/train-ticket-images.yaml", {(0, "job", "image"): images["train-ticket-deploy"]})
    update_yaml(
        f"{values}/flight-ticket-images.yaml",
        {
            (0, "jobs", job, "image"): images[f"flight-ticket-{target}"]
            for job, target in (
                ("deployActions", "action-deployer"),
                ("populateRedis", "populate-redis"),
                ("loadGenerator", "load-generator"),
            )
        },
    )
    update_yaml(
        f"{values}/astronomy-shop-fixes.yaml",
        {
            (0, "components", "load-generator", "sidecarContainers", 0, "imageOverride", field): value
            for field, value in zip(("repository", "tag"), parts["locust-exporter"], strict=True)
        },
    )

    social = "SREGym-applications/socialNetwork"
    chart = f"{social}/helm-chart/socialnetwork"
    update_yaml(f"{chart}/values.yaml", {(0, "global", "defaultImageVersion"): parts["social-network"][1]})
    for file in sorted((root / chart / "charts").glob("*/values.yaml")):
        name = yaml.safe_load(file.read_text()).get("name", "")
        if not (name.endswith("-service") or name in {"nginx-thrift", "media-frontend"}):
            continue
        target = {"nginx-thrift": "openresty-thrift", "media-frontend": "media-frontend"}.get(name, "social-network")
        repository, tag = parts[target]
        registry, image = repository.split("/", 1)
        updates = {(0, "container", "dockerRegistry"): registry, (0, "container", "image"): image}
        if target != "social-network":
            updates[(0, "container", "imageVersion")] = tag
        update_yaml(file, updates)
    update_dockerfile(f"{social}/Dockerfile", "ARG SOCIAL_NETWORK_BASE_IMAGE=", images["social-network-deps"])
    update_dockerfile("docker/hotel-reservation/release2.Dockerfile", "FROM ", images["social-network"])
    update_dockerfile(
        "SREGym-applications/flight-ticket/deploy_ow_actions/Dockerfile",
        "ARG PYTHON_RUNTIME_IMAGE=",
        images["flight-ticket-python-runtime"],
    )
    return outputs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Report drift without changing files")
    args = parser.parse_args()
    images = json.loads((ROOT / "docker/images.lock.json").read_text())
    outputs = render_consumers(ROOT, images)
    changed = [file for file, content in outputs.items() if file.read_text() != content]
    for file in changed:
        print(f"{'OUTDATED' if args.check else 'UPDATED'} {file.relative_to(ROOT)}")
        if not args.check:
            file.write_text(outputs[file])
    return int(args.check and bool(changed))


if __name__ == "__main__":
    raise SystemExit(main())

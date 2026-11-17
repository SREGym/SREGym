"""In-house programs shipped as container images, the way a company ships its own services.

A program carried in a Deployment's command can be read with ``kubectl get -o
yaml``; one baked into an image can be read only from inside the container,
which is where a real service's code is. Images are built on each node with
Docker and, where the kubelet runs on containerd rather than Docker, imported
into it; nothing is pushed anywhere, and pods use them with ``imagePullPolicy:
Never``. A tag is the hash of what the image holds, so a changed program can
never run under an old image.

Build them on every node before deploying (needs Docker; containerd nodes also
need passwordless sudo):

    uv run python -m sregym.service.apps.mattermost.in_house --tier 1b
"""

import argparse
import hashlib
import json
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REGISTRY = "registry.internal"
BASE_IMAGE = "python:3.12.13-alpine3.23"


@dataclass(frozen=True)
class InHouseImage:
    repository: str
    #: Path under /srv -> file content.
    files: dict = field(hash=False)
    #: Files under /srv that run directly.
    executables: tuple = ()
    #: A complete Dockerfile, for an image built from upstream source rather than from ``files``.
    recipe: str = ""

    @property
    def tag(self):
        content = [BASE_IMAGE, sorted(self.files.items()), self.executables] + ([self.recipe] if self.recipe else [])
        return hashlib.sha256(json.dumps(content).encode()).hexdigest()[:12]

    @property
    def reference(self):
        return f"{REGISTRY}/{self.repository}:{self.tag}"

    def dockerfile(self):
        if self.recipe:
            return self.recipe
        lines = [f"FROM {BASE_IMAGE}", "COPY srv/ /srv/", "WORKDIR /srv"]
        if self.executables:
            lines.append("RUN chmod 0755 " + " ".join(f"/srv/{name}" for name in self.executables))
        return "\n".join(lines) + "\n"

    def write_context(self, directory):
        directory = Path(directory)
        (directory / "srv").mkdir(parents=True, exist_ok=True)
        for name, content in self.files.items():
            (directory / "srv" / name).write_text(content)
        (directory / "Dockerfile").write_text(self.dockerfile())


#: The kubelet reports at most this many images per node (``nodeStatusMaxImages``).
REPORTED_IMAGES_CAP = 50


def node_images(kubectl=("kubectl",)):
    """Every image name the nodes report holding, or None when a node's list may be cut short."""
    nodes = json.loads(subprocess.run([*kubectl, "get", "nodes", "-o", "json"], capture_output=True, check=True).stdout)
    if any(len(node["status"].get("images", [])) >= REPORTED_IMAGES_CAP for node in nodes["items"]):
        return None
    return {name for node in nodes["items"] for image in node["status"].get("images", []) for name in image["names"]}


def missing(images, present):
    """References not on the nodes; nothing when the nodes' lists are incomplete."""
    if present is None:
        return []
    return sorted(image.reference for image in images if image.reference not in present)


def kubelet_uses_docker(kubectl=("kubectl",)):
    runtime = subprocess.run(
        [*kubectl, "get", "nodes", "-o", "jsonpath={.items[0].status.nodeInfo.containerRuntimeVersion}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return runtime.startswith("docker://")


def build_and_import(image, docker_runtime):
    """Build with Docker; on a containerd node, import into its Kubernetes namespace too."""
    with tempfile.TemporaryDirectory() as directory:
        image.write_context(directory)
        subprocess.run(["docker", "build", "-q", "-t", image.reference, directory], check=True)
    if docker_runtime:
        return
    saved = subprocess.Popen(["docker", "save", image.reference], stdout=subprocess.PIPE)
    subprocess.run(["sudo", "-n", "ctr", "-n", "k8s.io", "images", "import", "-"], stdin=saved.stdout, check=True)
    saved.stdout.close()
    if saved.wait():
        raise subprocess.CalledProcessError(saved.returncode, "docker save")


if __name__ == "__main__":
    from sregym.service.apps.mattermost.transit_cascade import transit_application

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tier", default="1b")
    args = parser.parse_args()
    docker_runtime = kubelet_uses_docker()
    for image in transit_application(args.tier).in_house_images():
        build_and_import(image, docker_runtime)
        print(image.reference, flush=True)

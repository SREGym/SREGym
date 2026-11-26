"""In-house programs shipped as container images, the way a company ships its own services.

A program carried in a Deployment's command can be read with ``kubectl get -o
yaml``; one baked into an image can be read only from inside the container,
which is where a real service's code is. Each image's build context is generated
from the runtime sources into ``docker/apps/mattermost/<name>`` and built with
the repository's other images (``docker/images.hcl``, group
``mattermost-fleets``). A released image is read from
``docker/images.lock.json``; until one is recorded, pods use the tag a local
``docker buildx bake`` gives it.

Regenerate the contexts after changing a runtime program:

    uv run python -m sregym.service.apps.mattermost.in_house
"""

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from sregym.paths import BASE_PARENT_DIR
from sregym.service.apps.helpers import image_reference

BASE_IMAGE = "python:3.12.13-alpine3.23"
CONTEXTS = BASE_PARENT_DIR / "docker" / "apps" / "mattermost"
REPOSITORY = "ghcr.io/sregym/mattermost"
#: ``IMAGE_TAG`` in ``docker/images.hcl`` when nothing overrides it.
LOCAL_TAG = "local"


@dataclass(frozen=True)
class InHouseImage:
    #: Build context directory, image tag suffix, and ``mattermost-<name>`` in the lock file.
    name: str
    #: Path under /srv -> file content.
    files: dict = field(hash=False)
    #: Files under /srv that run directly.
    executables: tuple = ()
    #: A complete Dockerfile, for an image built from upstream source rather than from ``files``.
    recipe: str = ""

    @property
    def reference(self):
        try:
            return image_reference(f"mattermost-{self.name}")
        except ValueError:
            return f"{REPOSITORY}:{LOCAL_TAG}-{self.name}"

    def dockerfile(self):
        if self.recipe:
            return self.recipe
        lines = [f"FROM {BASE_IMAGE}", "COPY srv/ /srv/", "WORKDIR /srv"]
        if self.executables:
            lines.append("RUN chmod 0755 " + " ".join(f"/srv/{name}" for name in self.executables))
        return "\n".join(lines) + "\n"

    def context(self):
        """Relative path -> content of everything the build reads."""
        return {"Dockerfile": self.dockerfile(), **{f"srv/{name}": content for name, content in self.files.items()}}


def committed_context(image, root=CONTEXTS):
    directory = Path(root) / image.name
    return {str(path.relative_to(directory)): path.read_text() for path in directory.rglob("*") if path.is_file()}


def write_context(image, root=CONTEXTS):
    directory = Path(root) / image.name
    shutil.rmtree(directory, ignore_errors=True)
    for relative, content in image.context().items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def all_images():
    """Tier 1b's and tier 2's images; tier 1 ships its programs in pod specs."""
    from sregym.service.apps.mattermost.transit_cascade import transit_application

    images = {}
    for tier in ("1b", "2"):
        for image in transit_application(tier).in_house_images():
            images[image.name] = image
    return list(images.values())


if __name__ == "__main__":
    from unittest.mock import patch

    from sregym.service.kubectl import KubeCtl

    # Rendering needs no cluster.
    with patch.object(KubeCtl, "__init__", lambda self: None):
        for image in all_images():
            write_context(image)
            print(CONTEXTS / image.name, flush=True)

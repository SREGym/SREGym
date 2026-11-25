"""Build optional image-only Harbor layers from a validated cluster's inventory.

Only registry content is bundled: every trial still creates its own cluster,
deploys its application, captures a healthy baseline and injects its fault.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIGEST = re.compile(r"sha256:([0-9a-f]{64})\Z")
IMAGE_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]*\Z")
INDEX_TYPE = "application/vnd.oci.image.index.v1+json"
MANIFEST_TYPE = "application/vnd.docker.distribution.manifest.v2+json"


def digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def checked_digest(value: str) -> str:
    match = DIGEST.fullmatch(value)
    if not match:
        raise ValueError(f"Expected a SHA-256 digest, got {value!r}")
    return match[1]


def check_blob(path: Path, digest: str, size: int | None = None) -> None:
    checked_digest(digest)
    with path.open("rb") as handle:
        actual = "sha256:" + hashlib.file_digest(handle, "sha256").hexdigest()
    if actual != digest or (size is not None and path.stat().st_size != size):
        raise ValueError(f"Registry blob failed digest/size verification: {path}")


def inventory(paths: list[Path], architecture: str) -> dict:
    """Read `kubectl get nodes -o json`, retaining aliases and platform identity."""
    images = {}
    aliases = {}
    nodes_seen = 0
    for path in paths:
        for node in json.loads(path.read_text())["items"]:
            nodes_seen += 1
            info = node["status"]["nodeInfo"]
            if (info["operatingSystem"], info["architecture"]) != ("linux", architecture):
                raise ValueError(f"{path}: node does not match linux/{architecture}")
            role = "common" if node["metadata"]["name"] == "kind-control-plane" else "workers"
            for image in node["status"].get("images", []):
                names = sorted(image["names"])
                source = next((name for name in names if "@sha256:" in name), None)
                if source is None:
                    raise ValueError(f"No immutable registry reference in {names}")
                digest = source.rsplit("@", 1)[1]
                checked_digest(digest)
                entry = images.setdefault(source, {"aliases": set(), "roles": set()})
                for name in names:
                    if not IMAGE_REFERENCE.fullmatch(name):
                        raise ValueError(f"Invalid image reference: {name!r}")
                    if "@" in name and name.rsplit("@", 1)[1] != digest:
                        raise ValueError(f"Image aliases disagree on their digest: {names}")
                    if name in aliases and aliases[name] != digest:
                        raise ValueError(f"Image tag changed between inventory snapshots: {name}")
                    aliases[name] = digest
                entry["aliases"].update(names)
                entry["roles"].add(role)
    if not nodes_seen or not images:
        raise ValueError("The node inventory contains no images")
    return {
        "schema_version": 1,
        "architecture": architecture,
        "images": {
            source: {
                "aliases": sorted(entry["aliases"]),
                "role": "common" if "common" in entry["roles"] else "workers",
            }
            for source, entry in sorted(images.items())
        },
    }


class Registry:
    """Use skopeo for authentication and transport, verifying original content."""

    def __init__(self, cache: Path, architecture: str):
        self.cache = cache
        self.architecture = architecture

    def manifest(self, reference: str) -> tuple[Path, dict]:
        digest = reference.rsplit("@", 1)[1]
        path = self.cache / "manifests" / checked_digest(digest)
        if not path.exists():
            result = subprocess.run(
                ["skopeo", "inspect", "--raw", "docker://" + reference],
                check=True,
                capture_output=True,
                timeout=180,
            )
            if digest_bytes(result.stdout) != digest:
                raise ValueError(f"Registry returned a different manifest for {reference}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(result.stdout)
        check_blob(path, digest)
        return path, json.loads(path.read_bytes())

    def content(self, reference: str, manifest: dict) -> Path:
        digest = reference.rsplit("@", 1)[1]
        directory = self.cache / "images" / checked_digest(digest)
        descriptors = [manifest["config"], *manifest["layers"]]
        if not all((directory / checked_digest(item["digest"])).exists() for item in descriptors):
            directory.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [
                    "skopeo",
                    "--override-os",
                    "linux",
                    "--override-arch",
                    self.architecture,
                    "copy",
                    "docker://" + reference,
                    "dir:" + str(directory),
                ],
                check=True,
                timeout=1800,
            )
        # OCI output in older skopeo versions can rewrite manifest media types.
        # Keep original raw manifests and verify the config/layer bytes instead.
        for item in descriptors:
            check_blob(directory / checked_digest(item["digest"]), item["digest"], item["size"])
        return directory


def image_content(registry: Registry, reference: str, architecture: str) -> tuple[dict, dict[str, Path]]:
    """Preserve the original index but include only the requested platform's blobs."""
    repository, root_digest = reference.rsplit("@", 1)
    digest = root_digest
    blobs = {}
    root = None
    for _ in range(8):
        path, manifest = registry.manifest(repository + "@" + digest)
        blobs[digest] = path
        media_type = manifest.get("mediaType", INDEX_TYPE if "manifests" in manifest else MANIFEST_TYPE)
        descriptor = {"mediaType": media_type, "digest": digest, "size": path.stat().st_size}
        if root is None:
            root = descriptor
        if "manifests" not in manifest:
            directory = registry.content(repository + "@" + digest, manifest)
            config = json.loads((directory / checked_digest(manifest["config"]["digest"])).read_bytes())
            if (config.get("os"), config.get("architecture")) != ("linux", architecture):
                raise ValueError(f"Image configuration does not match linux/{architecture}: {reference}")
            for item in [manifest["config"], *manifest["layers"]]:
                blobs[item["digest"]] = directory / checked_digest(item["digest"])
            assert root is not None
            return root, blobs
        matches = [
            item
            for item in manifest["manifests"]
            if item.get("platform", {}).get("os") == "linux"
            and item.get("platform", {}).get("architecture") == architecture
        ]
        if len(matches) != 1:
            raise ValueError(f"Expected one linux/{architecture} image in {reference}, found {len(matches)}")
        digest = matches[0]["digest"]
        checked_digest(digest)
    raise ValueError(f"Manifest nesting is too deep: {reference}")


def import_tags(reference: str, aliases: list[str]) -> list[str]:
    """k3s retags imported names, so only NamedTagged entries go in the index.

    CRI derives repository-digest aliases from these tags' unchanged targets.
    Digest-only images get a deterministic tag in their original repository.
    """

    def repository(name: str) -> str:
        name = name.split("@", 1)[0]
        return name[: name.rfind(":")] if name.rfind(":") > name.rfind("/") else name

    tags = {name for name in aliases if "@" not in name and name.rfind(":") > name.rfind("/")}
    repositories = {repository(name) for name in [reference, *aliases]}
    tagged = {repository(name) for name in tags}
    digest = checked_digest(reference.rsplit("@", 1)[1])
    tags.update(f"{name}:sregym-preload-{digest}" for name in repositories - tagged)
    return sorted(tags)


def write_archive(path: Path, images: dict, registry: Registry, architecture: str) -> None:
    descriptors = []
    blobs = {}
    for source, image in sorted(images.items()):
        print(f"Bundling {source}", flush=True)
        descriptor, content = image_content(registry, source, architecture)
        blobs.update(content)
        for alias in import_tags(source, image["aliases"]):
            descriptors.append(
                {
                    **descriptor,
                    "annotations": {
                        "io.containerd.image.name": alias,
                        "org.opencontainers.image.ref.name": alias,
                    },
                }
            )
    metadata = {
        "oci-layout": b'{"imageLayoutVersion":"1.0.0"}',
        "index.json": json.dumps({"schemaVersion": 2, "manifests": descriptors}, sort_keys=True).encode(),
    }
    temporary = path.with_suffix(".tar.tmp")
    with tarfile.open(temporary, "w", format=tarfile.PAX_FORMAT) as archive:
        for name, data in metadata.items():
            item = tarfile.TarInfo(name)
            item.size, item.mode = len(data), 0o600
            archive.addfile(item, io.BytesIO(data))
        for digest, blob in sorted(blobs.items()):
            check_blob(blob, digest)
            item = tarfile.TarInfo("blobs/sha256/" + checked_digest(digest))
            item.size, item.mode = blob.stat().st_size, 0o600
            with blob.open("rb") as handle:
                archive.addfile(item, handle)
    temporary.chmod(0o600)
    temporary.replace(path)
    os.utime(path, (0, 0))


def build_context(plan: dict, output: Path, cache: Path, base_image: str) -> None:
    if not IMAGE_REFERENCE.fullmatch(base_image):
        raise ValueError(f"Invalid base image reference: {base_image!r}")
    archives = output / "archives"
    archives.mkdir(parents=True, exist_ok=True)
    registry = Registry(cache, plan["architecture"])
    scripts = {
        "start.sh": ROOT / "docker/harbor/start.sh",
        "node.sh": ROOT / "docker/userns-k3s/node.sh",
        "preload-aliases.py": ROOT / "docker/userns-k3s/preload-aliases.py",
    }
    report = {
        **plan,
        "images": {
            source: {**image, "import_tags": import_tags(source, image["aliases"])}
            for source, image in plan["images"].items()
        },
        "base_image": base_image,
        "startup_scripts": {name: hashlib.sha256(source.read_bytes()).hexdigest() for name, source in scripts.items()},
        "archives": {},
    }
    for role in ("common", "workers"):
        images = {source: image for source, image in plan["images"].items() if image["role"] == role}
        path = archives / f"{role}.tar"
        if not images:
            path.unlink(missing_ok=True)
            continue
        write_archive(path, images, registry, plan["architecture"])
        with path.open("rb") as handle:
            checksum = hashlib.file_digest(handle, "sha256").hexdigest()
        report["archives"][role] = {"sha256": checksum, "bytes": path.stat().st_size}
    bundle = json.dumps(report, indent=2) + "\n"
    (output / "bundle.json").write_text(bundle)
    for script, source in scripts.items():
        shutil.copyfile(source, output / script)
    # Keep identical common content in a reusable layer across applications.
    archive_copies = "".join(
        f"COPY --chmod=600 archives/{role}.tar /opt/sregym-preload/{role}.tar\n"
        for role in ("common", "workers")
        if role in report["archives"]
    )
    (output / "Dockerfile").write_text(
        f"FROM {base_image}\n"
        f"LABEL sregym.preload.manifest-sha256={hashlib.sha256(bundle.encode()).hexdigest()}\n"
        "RUN install -d -m 700 /opt/sregym-preload\n"
        f"{archive_copies}"
        "COPY --chmod=600 bundle.json /opt/sregym-preload/bundle.json\n"
        "COPY --chmod=755 start.sh /usr/local/lib/sregym-harbor/start.sh\n"
        "COPY --chmod=755 node.sh /usr/local/lib/userns-k3s/node.sh\n"
        "COPY --chmod=755 preload-aliases.py /usr/local/lib/userns-k3s/preload-aliases.py\n"
    )
    (output / ".dockerignore").write_text(
        "*\n!Dockerfile\n!archives/\n!archives/*.tar\n!bundle.json\n!start.sh\n!node.sh\n!preload-aliases.py\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", nargs="+", type=Path, required=True, help="kubectl get nodes -o json snapshots")
    parser.add_argument("--architecture", choices=("amd64", "arm64"), default="amd64")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/sregym-preload")
    parser.add_argument("--base-image", required=True)
    parser.add_argument("--plan", action="store_true", help="Write only the inventory; do not download images")
    parser.add_argument("--build", action="store_true", help="Build the resulting Docker context")
    parser.add_argument("--tag", help="Docker image tag, required with --build")
    args = parser.parse_args()
    if args.build and (args.plan or not args.tag):
        parser.error("--build needs --tag and cannot be combined with --plan")
    plan = inventory(args.nodes, args.architecture)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    if not args.plan:
        build_context(plan, args.output_dir, args.cache_dir, args.base_image)
    if args.build:
        subprocess.run(
            ["docker", "build", "--platform", f"linux/{args.architecture}", "-t", args.tag, str(args.output_dir)],
            check=True,
        )


if __name__ == "__main__":
    main()

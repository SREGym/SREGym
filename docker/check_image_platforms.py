"""Verify that registry images offer both Linux architectures required by Lite.

Accept image references directly or extract containers (including init containers)
from rendered Kubernetes YAML. Registry errors fail closed; a locally loaded
image is never evidence that the published image supports an architecture.
"""

import argparse
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REQUIRED_PLATFORMS = {"linux/amd64", "linux/arm64"}


def container_images(document: object) -> set[str]:
    images: set[str] = set()
    if isinstance(document, dict):
        for key, value in document.items():
            if key in {"containers", "initContainers", "ephemeralContainers"} and isinstance(value, list):
                images.update(item["image"] for item in value if isinstance(item, dict) and item.get("image"))
            images.update(container_images(value))
    elif isinstance(document, list):
        for item in document:
            images.update(container_images(item))
    return images


def index_platforms(index: dict) -> set[str]:
    return {
        f"{platform.get('os')}/{platform.get('architecture')}"
        for manifest in index.get("manifests", [])
        if (platform := manifest.get("platform", {})).get("os") == "linux"
    }


def check_image(image: str) -> tuple[str, str | None]:
    try:
        result = subprocess.run(
            ["docker", "buildx", "imagetools", "inspect", "--raw", image],
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )
        index = json.loads(result.stdout)
        if "manifests" not in index:
            return image, "single-platform manifest; a multiarch index is required"
        missing = REQUIRED_PLATFORMS - index_platforms(index)
        return image, f"missing {', '.join(sorted(missing))}" if missing else None
    except (subprocess.SubprocessError, OSError, ValueError) as error:
        detail = error.stderr.strip() if isinstance(error, subprocess.CalledProcessError) else str(error)
        return image, f"registry inspection failed: {detail}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("images", nargs="*")
    parser.add_argument("--manifest", type=Path, action="append", default=[], help="Rendered YAML file or directory")
    args = parser.parse_args()
    images = set(args.images)
    if args.manifest:
        import yaml  # Available via `uv run`; direct image checks need only stdlib.

        for path in args.manifest:
            files = sorted([*path.rglob("*.yaml"), *path.rglob("*.yml")]) if path.is_dir() else [path]
            for file in files:
                for document in yaml.safe_load_all(file.read_text()):
                    images.update(container_images(document))
    if not images:
        parser.error("No container images found; pass references or --manifest")
    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(check_image, sorted(images)))
    for image, error in results:
        print(f"{'FAIL' if error else 'PASS'} {image}" + (f": {error}" if error else ""))
    return int(any(error for _, error in results))


if __name__ == "__main__":
    raise SystemExit(main())

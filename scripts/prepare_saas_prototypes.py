"""Build the licensed Stripe reference adapter inside the private DinD daemon."""

import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from sregym.service.apps.stripe_marathon import STRIPE_IMAGE

ROOT = Path(__file__).resolve().parents[1]


def prepare():
    fixtures = ROOT / "sregym/service/apps/fixtures/swe-marathon-stripe"
    manifest = json.loads((fixtures / "upstream.json").read_text())
    for record in manifest["files"]:
        if hashlib.sha256((fixtures / record["path"]).read_bytes()).hexdigest() != record["sha256"]:
            raise RuntimeError(f"Pinned upstream file was modified: {record['path']}")
    if not Path("/run/sregym-ready").is_file():
        raise RuntimeError("Run this command inside a ready SREGym DinD environment")
    subprocess.run(["python", "scripts/install_cnpg.py"], cwd=ROOT, check=True)
    # Do not send the full benchmark checkout, virtualenv or results as context.
    with tempfile.TemporaryDirectory(prefix="stripe-build-") as temporary:
        context = Path(temporary)
        shutil.copytree(fixtures, context / "sregym/service/apps/fixtures/swe-marathon-stripe")
        shutil.copytree(ROOT / "docker/stripe-marathon", context / "docker/stripe-marathon")
        subprocess.run(
            ["docker", "build", "-t", STRIPE_IMAGE, "-f", "docker/stripe-marathon/Dockerfile", "."],
            cwd=context,
            check=True,
        )
    subprocess.run(["kind", "load", "docker-image", STRIPE_IMAGE], check=True)


if __name__ == "__main__":
    prepare()

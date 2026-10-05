"""Fetch pinned upstream Linux/amd64 tools, verifying published SHA-256 sums."""

import hashlib
import io
import json
import urllib.request
import zipfile
from pathlib import Path

VERSIONS = {"consul": "1.10.4", "nomad": "1.9.7", "vault": "1.18.5"}


def main():
    target = Path(__file__).resolve().parent / "bin"
    locked = json.loads((target.parent / "toolchain.json").read_text())
    target.mkdir(exist_ok=True)
    for name, version in VERSIONS.items():
        if (target / name).exists() and hashlib.sha256((target / name).read_bytes()).hexdigest() == locked[name][
            "binary_sha256"
        ]:
            print(f"Verified existing {name} {version}")
            continue
        base = f"https://releases.hashicorp.com/{name}/{version}/"
        archive = f"{name}_{version}_linux_amd64.zip"
        with urllib.request.urlopen(base + f"{name}_{version}_SHA256SUMS", timeout=60) as response:
            checksums = response.read().decode()
        expected = next(line.split()[0] for line in checksums.splitlines() if line.split()[-1] == archive)
        if expected != locked[name]["archive_sha256"]:
            raise ValueError(f"upstream checksum differs from checked-in lock: {archive}")
        with urllib.request.urlopen(base + archive, timeout=120) as response:
            content = response.read()
        if hashlib.sha256(content).hexdigest() != expected:
            raise ValueError(f"checksum mismatch: {archive}")
        with zipfile.ZipFile(io.BytesIO(content)) as zipped:
            (target / name).write_bytes(zipped.read(name))
        (target / name).chmod(0o755)
        print(f"Verified {name} {version}: {expected}")


if __name__ == "__main__":
    main()

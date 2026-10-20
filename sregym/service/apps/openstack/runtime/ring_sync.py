"""Keep this pod's Swift rings in step with the ring service.

The operators' ring builder is the source of truth: whatever ring it last wrote
is what every proxy and storage node uses within seconds. Swift servers reload a
ring when its file changes.
"""

import os
import sys
import time
import urllib.request

SOURCE = os.environ.get("RING_SOURCE", "http://swift-rings:8000")
SWIFT_DIR = os.environ.get("SWIFT_DIR", "/etc/swift")
RINGS = ("account", "container", "object")


def sync_once() -> list[str]:
    changed = []
    for ring in RINGS:
        name = f"{ring}.ring.gz"
        with urllib.request.urlopen(f"{SOURCE}/{name}", timeout=10) as response:
            data = response.read()
        path = os.path.join(SWIFT_DIR, name)
        try:
            with open(path, "rb") as handle:
                if handle.read() == data:
                    continue
        except FileNotFoundError:
            pass
        with open(path + ".tmp", "wb") as handle:
            handle.write(data)
        os.replace(path + ".tmp", path)
        changed.append(name)
    return changed


def main():
    once = "--once" in sys.argv
    while True:
        try:
            changed = sync_once()
            if changed:
                print(f"installed {', '.join(changed)} from {SOURCE}", flush=True)
            if once:
                return
        except Exception as exc:
            print(f"ring sync failed: {exc}", flush=True)
            if once:
                raise
        time.sleep(float(os.environ.get("RING_SYNC_INTERVAL_S", "10")))


if __name__ == "__main__":
    main()

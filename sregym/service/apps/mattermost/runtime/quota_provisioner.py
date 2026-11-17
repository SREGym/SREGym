"""Registration that first launches the instance with the provider.

Shipped composed with ``provisioner`` (see ``transit_cascade.composed_source``):
``register`` replaces the base one. Launching takes a place under the provider's
instance quota, held until the instance is gone; launching an instance that
already has its place takes nothing more, so a failed registration is simply
retried.
"""
# ruff: noqa: F821 -- DATA, lock, leases_held, fetch_configuration, resource, BACKEND, FETCHES, Server and
# Provisioner come from provisioner, composed in.

import json
import os
import time
import urllib.error
import urllib.request
import uuid

COMPUTE = os.environ.get("PROVISIONER_COMPUTE", "http://compute:8080/instances")
LAUNCH_TIMEOUT = float(os.environ.get("PROVISIONER_LAUNCH_TIMEOUT", "5"))


def launch(instance):
    request = urllib.request.Request(
        COMPUTE,
        data=json.dumps({"instance": instance}).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=LAUNCH_TIMEOUT) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read()).get("error")
        except ValueError:
            message = None
        raise RuntimeError(message or f"launch refused: HTTP {exc.code}") from None


def register(instance):
    launch(instance)
    leases = DATA / "leases"
    leases.mkdir(parents=True, exist_ok=True)
    lease = leases / f"{instance}.{uuid.uuid4().hex[:8]}"
    held = lease.open("w")
    with lock:
        leases_held[lease] = held
    held.write(f"{time.time()}\n")
    held.flush()
    fetch_configuration(instance)
    with (DATA / "registry.jsonl").open("a") as registry:
        registry.write(json.dumps({"instance": instance, "at": time.time()}) + "\n")
    with lock:
        leases_held.pop(lease).close()
    lease.unlink(missing_ok=True)


if __name__ == "__main__":
    DATA.mkdir(parents=True, exist_ok=True)
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    print(f"provisioner backend={BACKEND} fetches={FETCHES} open_files_limit={soft}", flush=True)
    Server(("", 8080), Provisioner).serve_forever()

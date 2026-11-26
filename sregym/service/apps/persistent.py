"""Namespace cleanup that lets storage provisioners finish reclaiming data."""

import json
import subprocess
import time


class PersistentNamespaceCleanupError(RuntimeError):
    """Namespace or volume reclamation has yet to be confirmed."""


def _owned_delete_volumes(namespace, response):
    volumes = json.loads(response)["items"]
    if not isinstance(volumes, list):
        raise ValueError("Malformed persistent volume list")
    return [
        p["metadata"]["name"]
        for p in volumes
        if (p["spec"].get("claimRef") or {}).get("namespace") == namespace
        and p["spec"].get("persistentVolumeReclaimPolicy") == "Delete"
    ]


def cleanup_persistent_namespace(app, *, timeout=None):
    if timeout is not None:
        return _observe_persistent_disposal(app, timeout)

    # Capture only this application's volumes before deleting its namespace.
    names = _owned_delete_volumes(app.namespace, app.command("get", "pv", "-o", "json"))
    app.command(
        "delete", "namespace", app.namespace, "--ignore-not-found", "--wait=true", "--timeout=180s", timeout=200
    )
    # Cluster reconciliation must not race the provisioner's deletion helpers.
    # Never delete PV objects directly or remove their protection finalizers.
    if names:
        app.command("wait", "--for=delete", *[f"pv/{name}" for name in names], "--timeout=180s", timeout=200)


def _observe_persistent_disposal(app, timeout):
    deadline = time.monotonic() + timeout

    def command(*args):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("cleanup deadline expired")
        return app.command(*args, timeout=min(15, remaining))

    def wait_for_read(read, description):
        while True:
            try:
                return read()
            except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    detail = getattr(exc, "stderr", None) or str(exc)
                    raise PersistentNamespaceCleanupError(f"{description}: {detail.strip()}") from exc
                time.sleep(min(2, remaining))

    namespace = f"namespace/{app.namespace}"
    names = wait_for_read(
        lambda: _owned_delete_volumes(app.namespace, command("get", "pv", "-o", "json")),
        f"Could not capture Delete volumes for {namespace}",
    )
    remaining_objects = {namespace, *(f"persistentvolume/{name}" for name in names)}
    delete_timeout = None
    try:
        command("delete", "namespace", app.namespace, "--ignore-not-found", "--wait=false")
    except subprocess.TimeoutExpired as exc:
        # The request may have reached the API before kubectl timed out.
        delete_timeout = exc
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", None) or str(exc)
        raise PersistentNamespaceCleanupError(f"Could not request deletion of {namespace}: {detail.strip()}") from exc

    def remaining():
        response = command("get", *sorted(remaining_objects), "--ignore-not-found", "-o", "name")
        observed = set(response.split())
        if not observed <= remaining_objects:
            raise ValueError(f"Unexpected cleanup response: {response!r}")
        return observed

    while remaining_objects:
        description = f"Disposal unconfirmed for {', '.join(sorted(remaining_objects))}"
        if delete_timeout is not None:
            description += f" (namespace deletion reply timed out: {delete_timeout})"
        remaining_objects = wait_for_read(remaining, description)
        if remaining_objects:
            time.sleep(min(2, max(0, deadline - time.monotonic())))

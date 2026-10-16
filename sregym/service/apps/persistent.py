"""Namespace cleanup that lets storage provisioners finish reclaiming data."""

import json


def cleanup_persistent_namespace(app):
    # Capture only this application's volumes before deleting its namespace.
    volumes = json.loads(app.command("get", "pv", "-o", "json"))["items"]
    names = [
        p["metadata"]["name"]
        for p in volumes
        if p["spec"].get("claimRef", {}).get("namespace") == app.namespace
        and p["spec"].get("persistentVolumeReclaimPolicy") == "Delete"
    ]
    app.command(
        "delete", "namespace", app.namespace, "--ignore-not-found", "--wait=true", "--timeout=180s", timeout=200
    )
    # Cluster reconciliation must not race the provisioner's deletion helpers.
    # Never delete PV objects directly or remove their protection finalizers.
    if names:
        app.command("wait", "--for=delete", *[f"pv/{name}" for name in names], "--timeout=180s", timeout=200)

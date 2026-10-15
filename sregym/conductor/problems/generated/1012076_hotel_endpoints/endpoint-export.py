#!/usr/bin/env python3
import argparse
import json
import os
import signal
import ssl
import threading
import urllib.error
import urllib.request

parser = argparse.ArgumentParser()
parser.add_argument("--state-dir", required=True)
args = parser.parse_args()
state_file = os.path.join(args.state_dir, "registration.json")
stopping = threading.Event()
signal.signal(signal.SIGTERM, lambda *_: stopping.set())
signal.signal(signal.SIGINT, lambda *_: stopping.set())
credentials = "/var/run/secrets/kubernetes.io/serviceaccount"
context = ssl.create_default_context(cafile=credentials + "/ca.crt")
base = "https://" + os.environ["KUBERNETES_SERVICE_HOST"] + ":" + os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")


def request(method, path, data=None):
    with open(credentials + "/token") as f:
        token = f.read().strip()
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        base + path,
        data=body,
        method=method,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, context=context, timeout=15) as response:
        return json.load(response)


def reconcile(config):
    ns = config["namespace"]
    service = request("GET", "/api/v1/namespaces/" + ns + "/services/" + config["service"])
    pods = request("GET", "/api/v1/namespaces/" + ns + "/pods")["items"]
    endpoints = []
    for pod in sorted(pods, key=lambda p: p["metadata"]["name"]):
        if pod["metadata"].get("labels", {}).get("io.kompose.service") not in config["members"]:
            continue
        if pod["metadata"].get("deletionTimestamp") or pod["status"].get("phase") != "Running":
            continue
        if not any(c["type"] == "Ready" and c["status"] == "True" for c in pod["status"].get("conditions", [])):
            continue
        address = pod["status"].get("podIP")
        if address:
            endpoints.append(
                {
                    "addresses": [address],
                    "conditions": {"ready": True, "serving": True, "terminating": False},
                    "nodeName": pod["spec"]["nodeName"],
                    "targetRef": {
                        "kind": "Pod",
                        "namespace": ns,
                        "name": pod["metadata"]["name"],
                        "uid": pod["metadata"]["uid"],
                    },
                }
            )
    port = next(p for p in service["spec"]["ports"] if p["port"] == 5000)
    obj = {
        "apiVersion": "discovery.k8s.io/v1",
        "kind": "EndpointSlice",
        "metadata": {
            "name": config["name"],
            "namespace": ns,
            "labels": {
                "io.kompose.service": config["service"],
                "kubernetes.io/service-name": config["service"],
                "endpointslice.kubernetes.io/managed-by": "consul.hashicorp.com",
            },
            "ownerReferences": [
                {
                    "apiVersion": "v1",
                    "kind": "Service",
                    "name": config["service"],
                    "uid": service["metadata"]["uid"],
                    "controller": True,
                    "blockOwnerDeletion": True,
                }
            ],
        },
        "addressType": "IPv4",
        "ports": [{"name": port["name"], "port": config["port"], "protocol": "TCP"}],
        "endpoints": endpoints,
    }
    path = "/apis/discovery.k8s.io/v1/namespaces/" + ns + "/endpointslices"
    try:
        current = request("GET", path + "/" + config["name"])
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        request("POST", path, obj)
    else:
        obj["metadata"]["resourceVersion"] = current["metadata"]["resourceVersion"]
        if current["metadata"].get("annotations"):
            obj["metadata"]["annotations"] = current["metadata"]["annotations"]
        request("PUT", path + "/" + config["name"], obj)


try:
    while not stopping.is_set():
        with open(state_file) as f:
            config = json.load(f)
        try:
            reconcile(config)
            interval = config.get("interval", 120)
        except Exception:
            interval = 10
        stopping.wait(interval)
finally:
    path = os.path.join(args.state_dir, "export.pid")
    if os.path.exists(path):
        os.unlink(path)

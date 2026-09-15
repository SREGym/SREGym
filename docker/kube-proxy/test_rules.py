"""Compare real proxy rule generation using a credential-free fixture API.

NET_ADMIN applies only to a disposable container's network namespace. This test
does not use host networking, a Kubernetes cluster, or any real credentials.
"""

import argparse
import json
import platform
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

SERVICE = {
    "apiVersion": "v1",
    "kind": "Service",
    "metadata": {"name": "backend", "namespace": "proxy-test", "uid": "service-1", "resourceVersion": "1"},
    "spec": {
        "type": "ClusterIP",
        "clusterIP": "10.96.0.100",
        "clusterIPs": ["10.96.0.100"],
        "ipFamilies": ["IPv4"],
        "sessionAffinity": "None",
        "ports": [{"name": "http", "protocol": "TCP", "port": 80, "targetPort": 8080}],
    },
}
NODE = {
    "apiVersion": "v1",
    "kind": "Node",
    "metadata": {"name": "proxy-test", "uid": "node-1", "resourceVersion": "1"},
    "spec": {"podCIDR": "10.244.0.0/24", "podCIDRs": ["10.244.0.0/24"]},
    "status": {"addresses": [{"type": "InternalIP", "address": "192.0.2.10"}]},
}
SLICE = {
    "apiVersion": "discovery.k8s.io/v1",
    "kind": "EndpointSlice",
    "metadata": {
        "name": "backend-1",
        "namespace": "proxy-test",
        "uid": "slice-1",
        "resourceVersion": "1",
        "labels": {"kubernetes.io/service-name": "backend"},
    },
    "addressType": "IPv4",
    "ports": [{"name": "http", "protocol": "TCP", "port": 8080}],
    "endpoints": [
        {"addresses": [f"10.244.0.{i + 10}"], "conditions": {"ready": True}, "nodeName": "proxy-test"} for i in range(5)
    ],
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == "/api/v1/nodes/proxy-test":
            body = NODE
        else:
            objects = {
                "/api/v1/nodes": ("v1", "NodeList", [NODE]),
                "/api/v1/services": ("v1", "ServiceList", [SERVICE]),
                "/apis/discovery.k8s.io/v1/endpointslices": ("discovery.k8s.io/v1", "EndpointSliceList", [SLICE]),
            }
            if url.path not in objects:
                self.send_error(404)
                return
            version, kind, items = objects[url.path]
            if parse_qs(url.query).get("watch") == ["true"]:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                try:
                    for item in items:
                        self.wfile.write(json.dumps({"type": "ADDED", "object": item}).encode() + b"\n")
                    self.wfile.flush()
                    self.server.done.wait(90)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            body = {"apiVersion": version, "kind": kind, "metadata": {"resourceVersion": "1"}, "items": items}
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        data = json.dumps({"apiVersion": "v1", "kind": "Status", "status": "Success", "code": 201}).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def docker(*args, check=True):
    return subprocess.run(["docker", *args], text=True, capture_output=True, check=check, timeout=60)


def check_rules(rules):
    matches = [line for line in rules.splitlines() if "--probability" in line and "proxy-test/backend:http" in line]
    if len(matches) != 4:
        return None  # The informers may not have finished their initial sync.
    probabilities = [float(re.search(r"--probability ([0-9.]+)", line).group(1)) for line in matches]
    # xt_statistic stores a fixed-point value, not the source float.
    assert all(abs(value - 0.0114514) < 1 / 2**31 for value in probabilities), matches
    chain = matches[0].split()[1]
    service_rules = [line for line in rules.splitlines() if line.startswith(f"-A {chain} ") and "-j KUBE-SEP-" in line]
    assert len(service_rules) == 5 and service_rules[:-1] == matches, service_rules
    assert "--probability" not in service_rules[-1] and "-j KUBE-SEP-" in service_rules[-1], service_rules
    endpoints = [line for line in rules.splitlines() if "--to-destination 10.244.0." in line]
    assert len(endpoints) == 5, endpoints
    return {"service_rules": service_rules, "endpoint_rules": endpoints}


def check_executable(name, arch):
    # Docker's platform metadata alone would allow an incorrectly labeled x86
    # executable to pass under emulation on an ARM host. Inspect the actual ELF.
    with tempfile.TemporaryDirectory(prefix="sregym-proxy-") as directory:
        binary = Path(directory) / "kube-proxy"
        docker("cp", f"{name}:/usr/local/bin/kube-proxy", str(binary))
        with binary.open("rb") as source:
            header = source.read(20)
        assert header[:6] == b"\x7fELF\x02\x01", "Expected a little-endian ELF64 executable"
        assert int.from_bytes(header[18:20], "little") == {"amd64": 62, "arm64": 183}[arch], header.hex()


def main(image, arch):
    listen_host = "127.0.0.1" if platform.system() == "Darwin" else "0.0.0.0"
    server = ThreadingHTTPServer((listen_host, 0), Handler)
    server.done = threading.Event()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    name = "sregym-proxy-" + uuid.uuid4().hex[:10]
    try:
        host_args = [] if platform.system() == "Darwin" else ["--add-host=host.docker.internal:host-gateway"]
        docker(
            "run",
            "-d",
            *host_args,
            "--name",
            name,
            "--label",
            "sregym.proxy-test=true",
            "--platform",
            "linux/" + arch,
            "--cap-add",
            "NET_ADMIN",
            "--memory",
            "256m",
            "--entrypoint",
            "/usr/local/bin/kube-proxy",
            image,
            "--master",
            f"http://host.docker.internal:{server.server_port}",
            "--hostname-override=proxy-test",
            "--bind-address=192.0.2.10",
            "--proxy-mode=iptables",
            "--cluster-cidr=10.244.0.0/16",
            "--iptables-sync-period=1s",
            "--iptables-min-sync-period=0s",
            "--iptables-localhost-nodeports=false",
            "--healthz-bind-address=",
            "--metrics-bind-address=",
            "--conntrack-max-per-core=0",
            "--conntrack-tcp-timeout-established=0s",
            "--conntrack-tcp-timeout-close-wait=0s",
        )
        check_executable(name, arch)
        deadline = time.monotonic() + 90
        rules = ""
        while time.monotonic() < deadline:
            rules = docker("exec", name, "iptables-save", "-t", "nat", check=False).stdout
            checked = check_rules(rules)
            if checked:
                print(json.dumps({"image": image, "arch": arch, "passed": True, **checked}, indent=2))
                return
            if docker("inspect", name, "--format", "{{.State.Running}}").stdout.strip() != "true":
                break
            time.sleep(1)
        print(rules, file=sys.stderr)
        print(docker("logs", name, check=False).stderr, file=sys.stderr)
        raise AssertionError("Proxy did not install the expected fixture rules")
    finally:
        try:
            docker("rm", "-f", name, check=False)
        finally:
            server.done.set()
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("arch", choices=["amd64", "arm64"])
    args = parser.parse_args()
    main(args.image, args.arch)

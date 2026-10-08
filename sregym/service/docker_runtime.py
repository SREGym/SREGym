"""Select trusted and workload Docker engines without changing process globals."""

import ipaddress
import json
import os
import socket
import stat
import subprocess
from pathlib import Path
from urllib.parse import urlsplit


def docker_command(*arguments: str, host: str | None = None) -> list[str]:
    return ["docker", *(["--host", host] if host else []), *arguments]


def trusted_docker_host() -> str | None:
    host = os.environ.get("SREGYM_TRUSTED_DOCKER_HOST") or None
    if rootless_workload_enabled() and not host:
        raise ValueError("Rootless runs require an explicit trusted Docker endpoint")
    return host


def rootless_workload_enabled() -> bool:
    return os.environ.get("SREGYM_ROOTLESS_WORKLOAD") == "1"


def runner_address() -> str:
    value = os.environ.get("SREGYM_RUNNER_ADDRESS", "")
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError as exc:
        raise ValueError("SREGYM_RUNNER_ADDRESS must be a reachable host IPv4 address") from exc
    if address.is_loopback or address.is_unspecified or address.is_multicast:
        raise ValueError("SREGYM_RUNNER_ADDRESS must be a reachable host IPv4 address")
    return str(address)


def _validate_rootless_inotify_budget() -> int:
    """Require Kind's host prerequisite before deploying or injecting faults."""
    try:
        instances = int(Path("/proc/sys/fs/inotify/max_user_instances").read_text().strip())
    except (OSError, ValueError) as exc:
        raise ValueError("Could not verify the Linux host's inotify instance budget") from exc
    if instances < 512:
        raise ValueError(
            "Rootless Kind requires fs.inotify.max_user_instances >= 512 during host provisioning; "
            "an exhausted shared UID budget can disable container OOM watching"
        )
    return instances


def _validate_workload_cluster(workload_host: str, address: str) -> list[str]:
    """Ensure kubectl cannot accidentally target a privileged sibling cluster."""
    filename = os.environ.get("KUBECONFIG", "")
    path = Path(filename)
    if not filename or not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError("Rootless runs require an explicit private workload KUBECONFIG")
    metadata = path.stat()
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        raise ValueError("The workload kubeconfig must be private and owned by the trusted runner")

    def kubectl(*args):
        result = subprocess.run(
            ["kubectl", "--kubeconfig", str(path), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return json.loads(result.stdout)

    cluster = kubectl("config", "view", "--minify", "-o", "json")["clusters"][0]["cluster"]
    endpoint = urlsplit(cluster["server"])
    if endpoint.scheme != "https" or endpoint.hostname != address or cluster.get("insecure-skip-tls-verify"):
        raise ValueError("The workload API must use verified HTTPS on SREGYM_RUNNER_ADDRESS")
    nodes = kubectl("get", "nodes", "-o", "json").get("items", [])
    if not nodes:
        raise ValueError("The rootless workload cluster has no nodes")
    names, control_plane_matches = [], False
    for node in nodes:
        name = node.get("metadata", {}).get("name", "")
        provider = node.get("spec", {}).get("providerID", "")
        if not name or not provider.startswith("kind://docker/") or provider.rsplit("/", 1)[-1] != name:
            raise ValueError("Rootless runs currently require Kind nodes from the workload engine")
        result = subprocess.run(
            docker_command("inspect", "--type", "container", name, host=workload_host),
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        container = json.loads(result.stdout)[0]
        role = container.get("Config", {}).get("Labels", {}).get("io.x-k8s.kind.role")
        if role not in {"control-plane", "worker"}:
            raise ValueError("A Kubernetes node does not belong to the selected rootless Kind engine")
        names.append(name)
        if role == "control-plane":
            ports = container.get("NetworkSettings", {}).get("Ports", {}).get("6443/tcp") or []
            control_plane_matches |= any(
                port.get("HostIp") == address and int(port.get("HostPort", "0")) == (endpoint.port or 443)
                for port in ports
            )
    if not control_plane_matches:
        raise ValueError("The workload kubeconfig does not match the rootless engine's API port")
    return sorted(names)


def validate_rootless_boundary() -> dict:
    """Fail before injection if the experimental engines share an identity.

    This checks deployment configuration, not immunity to kernel exploits or
    the trustworthiness of observations from an agent-controlled cluster.
    """
    if os.name != "posix":
        raise ValueError("The experimental rootless deployment requires a Linux host")
    if os.environ.get("DOCKER_CONTEXT"):
        raise ValueError("Rootless runs require DOCKER_CONTEXT to be unset so workload commands use DOCKER_HOST")
    workload_host = os.environ.get("DOCKER_HOST", "")
    trusted_host = trusted_docker_host()
    if not workload_host.startswith("unix://") or not trusted_host:
        raise ValueError("Rootless runs require explicit workload and trusted Docker endpoints")
    if workload_host == trusted_host:
        raise ValueError("Workload and trusted Docker endpoints must be distinct")
    info = Path(workload_host.removeprefix("unix://")).stat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid in {0, os.getuid()}:
        raise ValueError("The workload socket must belong to a separate unprivileged host account")
    if not os.environ.get("SREGYM_CLUSTER_BASELINE_FILE"):
        raise ValueError("Rootless runs require a dedicated SREGYM_CLUSTER_BASELINE_FILE")
    address = runner_address()
    with socket.socket() as probe:
        probe.bind((address, 0))
    engines = []
    for endpoint in (workload_host, trusted_host):
        result = subprocess.run(
            docker_command("info", "--format", "{{json .}}", host=endpoint),
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        engines.append(json.loads(result.stdout))
    workload, trusted = engines
    if not workload.get("ID") or workload["ID"] == trusted.get("ID"):
        raise ValueError("The workload and verifier must use separate Docker engines")
    if "name=rootless" not in workload.get("SecurityOptions", []) or str(workload.get("CgroupVersion")) != "2":
        raise ValueError("The workload engine must use rootless Docker and cgroup v2")
    inotify_instances = _validate_rootless_inotify_budget()
    nodes = _validate_workload_cluster(workload_host, address)
    return {
        "workload_engine": workload["ID"],
        "trusted_engine": trusted["ID"],
        "workload_uid": info.st_uid,
        "runner_address": address,
        "nodes": nodes,
        "inotify_max_user_instances": inotify_instances,
    }

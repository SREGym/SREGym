"""Deterministic source edit/build/redeploy/submit probe run by the Copilot CLI agent."""

import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

import requests

from sregym.service.db_build_spec import DB_REGISTRY
from sregym.service.generic_db_build_manager import GenericDBBuildManager

PROBLEM_ID = "auto_cassandra_20036"
VERSION = "5.0.2"
NAMESPACE = "k8ssandra-operator"
ROOT_CAUSE_FILE = "src/java/org/apache/cassandra/schema/TableMetadata.java"


def _run(cmd: str, *, check: bool = True, timeout: int | None = None) -> subprocess.CompletedProcess:
    print(f"[copilotcli-probe] $ {cmd}", flush=True)
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    if result.stdout:
        print(result.stdout, flush=True)
    if result.stderr:
        print(result.stderr, flush=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"Command failed ({result.returncode}): {cmd}\n{result.stdout}\n{result.stderr}")
    return result


def _api_base_url() -> str:
    host = os.getenv("API_HOSTNAME", "localhost")
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    return f"http://{host}:{os.getenv('API_PORT', '8000')}"


def _wait_for_status(stages: set[str], timeout: int = 300) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = requests.get(f"{_api_base_url()}/status", timeout=5)
        response.raise_for_status()
        stage = response.json().get("stage")
        if stage in stages:
            return stage
        time.sleep(1)
    raise TimeoutError(f"Timed out waiting for conductor stages {sorted(stages)}")


def _edit_source(source_path: Path) -> str:
    target = source_path / ROOT_CAUSE_FILE
    if not target.exists():
        raise FileNotFoundError(target)
    marker = f"// SREGym Copilot CLI rebuild probe marker: {datetime.now().isoformat()}\n"
    with target.open("a", encoding="utf-8") as f:
        f.write("\n" + marker)
    _run(f"git -C {source_path} --no-pager diff -- {ROOT_CAUSE_FILE}", check=False)
    print(f"[copilotcli-probe] Edited {target}", flush=True)
    return marker.strip()


def _build_image(source_path: Path) -> str:
    spec = DB_REGISTRY["cassandra"]
    image = GenericDBBuildManager(spec, source_path, VERSION).build_from_directory()
    print(f"[copilotcli-probe] Built and loaded image {image}", flush=True)
    return image


def _statefulsets() -> list[str]:
    out = _run(
        f"kubectl get statefulsets -n {NAMESPACE} "
        "-o jsonpath='{range .items[*]}{.metadata.name} {end}'",
    ).stdout
    sts = [item for item in out.strip().strip("'").split() if item]
    if not sts:
        raise RuntimeError(f"No StatefulSets found in namespace {NAMESPACE}")
    return sts


def _ensure_operator_webhooks() -> None:
    deployments = "deployment/cassandra-operator-cass-operator deployment/cassandra-operator-k8ssandra-operator"
    _run(f"kubectl scale {deployments} -n {NAMESPACE} --replicas=1")
    for deployment in ("cassandra-operator-k8ssandra-operator", "cassandra-operator-cass-operator"):
        _run(f"kubectl rollout status deployment/{deployment} -n {NAMESPACE} --timeout=180s", timeout=240)


def _set_desired_image(image: str) -> None:
    patch = json.dumps({"spec": {"cassandra": {"serverImage": image}}})
    _run(f"kubectl patch k8ssandracluster sregym-cassandra -n {NAMESPACE} --type=merge -p '{patch}'")


def _redeploy_image(image: str) -> list[str]:
    _ensure_operator_webhooks()
    _set_desired_image(image)
    patched = []
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    for sts in _statefulsets():
        patch = {
            "spec": {
                "updateStrategy": {"rollingUpdate": {"partition": 0}},
                "template": {
                    "metadata": {"annotations": {"sregym.copilot/redeploy": stamp}},
                    "spec": {"containers": [{"name": "cassandra", "image": image}]},
                },
            }
        }
        patch_json = json.dumps(patch)
        _run(f"kubectl patch statefulset {sts} -n {NAMESPACE} --type=strategic -p '{patch_json}'")
        _run(f"kubectl rollout status statefulset/{sts} -n {NAMESPACE} --timeout=1200s", timeout=1260)
        patched.append(sts)
    image_check = _run(
        f"kubectl get pods -n {NAMESPACE} "
        "-o jsonpath='{range .items[*]}{.metadata.name}{\"\\t\"}{range .spec.containers[*]}{.image}{\" \"}{end}{\"\\n\"}{end}'",
        check=False,
    ).stdout
    if image not in image_check:
        raise RuntimeError(f"Patched image {image} was not observed in pod specs:\n{image_check}")
    print(f"[copilotcli-probe] Redeployed image {image} to StatefulSets: {patched}", flush=True)
    return patched


def _submit(image: str, marker: str, patched: list[str]) -> dict:
    solution = (
        "Copilot CLI plumbing probe completed: edited source marker "
        f"{marker!r}, built image {image}, redeployed StatefulSets {patched}."
    )
    response = requests.post(f"{_api_base_url()}/submit", json={"solution": solution}, timeout=60)
    response.raise_for_status()
    print(f"[copilotcli-probe] Submit response: {response.text}", flush=True)
    return response.json()


def main() -> None:
    if os.getenv("SREGYM_PROBLEM_ID") not in {None, "", PROBLEM_ID}:
        raise RuntimeError(f"This probe is only intended for {PROBLEM_ID}")
    stage = _wait_for_status({"diagnosis", "mitigation"})
    print(f"[copilotcli-probe] Conductor ready at stage {stage}", flush=True)

    source_env = os.environ.get("SREGYM_SOURCE_CODE_PATH")
    if not source_env:
        raise RuntimeError("SREGYM_SOURCE_CODE_PATH is not set")
    source_path = Path(source_env).resolve()
    if not source_path.exists():
        raise FileNotFoundError(source_path)

    marker = _edit_source(source_path)
    image = _build_image(source_path)
    patched = _redeploy_image(image)
    submit_response = _submit(image, marker, patched)

    logs_dir = Path(os.environ.get("AGENT_LOGS_DIR", ".")).resolve()
    logs_dir.mkdir(parents=True, exist_ok=True)
    evidence = {
        "problem_id": PROBLEM_ID,
        "stage": stage,
        "source_path": str(source_path),
        "edited_file": str(source_path / ROOT_CAUSE_FILE),
        "marker": marker,
        "image": image,
        "statefulsets": patched,
        "submit_response": submit_response,
        "timestamp": datetime.now().isoformat(),
    }
    evidence_path = logs_dir / "copilotcli_probe_evidence.json"
    evidence_path.write_text(json.dumps(evidence, indent=2))
    print(f"[copilotcli-probe] Evidence written to {evidence_path}", flush=True)


if __name__ == "__main__":
    main()

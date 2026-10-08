"""Container-side oracle execution. Only the private protocol writes to stdout."""

import base64
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

MAX_FRAME_BYTES = 64 * 1024 * 1024


def read_frame(stream):
    line = stream.readline(MAX_FRAME_BYTES + 1)
    if not line or len(line) > MAX_FRAME_BYTES or not line.endswith(b"\n"):
        raise ValueError("Missing or oversized verifier protocol frame")
    return json.loads(line)


def write_frame(stream, message):
    stream.write(json.dumps(message, allow_nan=False).encode() + b"\n")
    stream.flush()


class RemoteWorkload:
    """Keep existing host traffic running; transport only its narrow IO calls."""

    def __init__(self, index, call):
        self.index = index
        self.call = call
        self.metrics = SimpleNamespace(snapshot=lambda: call(index, "metrics", []))

    def snapshot(self, window_seconds=20.0):
        return SimpleNamespace(**self.call(self.index, "snapshot", [window_seconds]))

    def start(self):
        return self.call(self.index, "start", [])

    def stop(self):
        return self.call(self.index, "stop", [])

    def set_rate(self, rate):
        return self.call(self.index, "set_rate", [rate])

    def inference(self, messages):
        from langchain_core.messages import messages_to_dict

        return SimpleNamespace(content=self.call(self.index, "model_inference", [messages_to_dict(messages)]))


def execute_oracle(oracle, args=()):
    """Run grading inside the worker and preserve oracle failure classifications."""
    from sregym.conductor.oracles.base import Oracle

    try:
        result = oracle.evaluate(*args)
    except Exception as exc:
        result = {**Oracle.fail_from_exception(exc), "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(result, dict) or type(result.get("success")) is not bool:
        raise ValueError("Oracle must return a verdict with boolean success")
    return result


def main():
    # Redirect fd 1 as well as Python stdout: output inherited by subprocesses
    # and pod logs must never masquerade as a verdict or resource request.
    channel = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    request = read_frame(sys.stdin.buffer)
    run_id = request["run_id"]
    home = Path("/tmp/verifier-home")
    home.mkdir(mode=0o700, exist_ok=True)
    os.environ["HOME"] = str(home)
    kubeconfig = home / "kubeconfig.json"
    kubeconfig.write_text(json.dumps(request["kubeconfig"]))
    kubeconfig.chmod(0o600)
    os.environ["KUBECONFIG"] = str(kubeconfig)
    os.environ["SREGYM_VERIFIER_CONTAINER"] = "1"

    from kubernetes import config

    from sregym.conductor.oracles.base import Oracle
    from sregym.service.verifier_state import restore_oracle

    def call(index, operation, args):
        write_frame(channel, {"run_id": run_id, "type": "resource", "index": index, "op": operation, "args": args})
        response = read_frame(sys.stdin.buffer)
        if response.get("run_id") != run_id or response.get("type") != "resource_result":
            raise ValueError("Mismatched verifier resource response")
        if "error" in response:
            raise RuntimeError(response["error"])
        return response["value"]

    try:
        config.load_kube_config()
        payload = base64.b64decode(request["oracle"], validate=True)
        oracle = restore_oracle(payload, lambda index: RemoteWorkload(index, call))
        if not isinstance(oracle, Oracle):
            raise TypeError("Snapshot does not contain an Oracle")
        result = execute_oracle(oracle, request.get("args", []))
        write_frame(channel, {"run_id": run_id, "type": "verdict", "result": result})
    except BaseException as exc:
        write_frame(channel, {"run_id": run_id, "type": "error", "error": f"{type(exc).__name__}: {exc}"})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

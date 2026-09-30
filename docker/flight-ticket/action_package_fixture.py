"""Exercise one shipped ZIP through the real Python 3.6 OpenWhisk proxy."""

import base64
import json
import os
import platform
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


def post(endpoint, value):
    request = urllib.request.Request(
        "http://127.0.0.1:8080/" + endpoint,
        json.dumps({"value": value}).encode(),
        {"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        assert response.status == 200, response.status
        body = response.read()
        # This legacy proxy returns plain-text "OK" for /init, JSON for /run.
        return json.loads(body) if endpoint == "run" else None


expected_machine = {"amd64": "x86_64", "arm64": "aarch64"}[os.environ["EXPECTED_ARCH"]]
assert platform.machine() == expected_machine, platform.machine()
action = os.environ["ACTION"]
archive = Path("/packages") / action / "function.zip"
server = subprocess.Popen([sys.executable, "-u", "pythonrunner.py"], cwd="/pythonAction")
try:
    deadline = time.monotonic() + 30
    while True:
        if server.poll() is not None:
            raise RuntimeError("OpenWhisk proxy exited before initialization")
        try:
            with socket.create_connection(("127.0.0.1", 8080), timeout=1):
                break
        except OSError as error:
            if time.monotonic() >= deadline:
                raise RuntimeError("OpenWhisk proxy startup timed out") from error
            time.sleep(0.2)
    post("init", {"binary": True, "main": "main", "code": base64.b64encode(archive.read_bytes()).decode("ascii")})
    result = {"action": action, "architecture": platform.machine(), "init": 200}
    params = {"REDIS_HOST": "redis", "REDIS_PORT": 6379}
    if action == "QueryForStationIdByStationName":
        assert post("run", dict(params, stationName="PackagingTest")) == {"Result": "1234"}
        result["redis_read"] = 200
    elif action == "Drawback":
        assert post("run", dict(params, loginId="packaging-test", money=3)) == {"Result": 1}
        result["redis_write"] = 200
    print(json.dumps(result), flush=True)
finally:
    server.terminate()
    try:
        server.wait(timeout=5)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait()

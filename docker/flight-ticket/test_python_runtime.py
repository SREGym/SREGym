"""Exercise the OpenWhisk ZIP-action /init and /run contract inside an image.

Run through docker/test_image.sh so the selected architecture is checked too.
Keep this test compatible with the original Python 3.6 runtime.
"""

import base64
import io
import json
import socket
import subprocess
import sys
import time
import urllib.request
import zipfile

server = subprocess.Popen([sys.executable, "-u", "pythonrunner.py"], cwd="/pythonAction")
try:
    deadline = time.monotonic() + 20
    while True:
        if server.poll() is not None:
            raise RuntimeError("OpenWhisk proxy exited during startup")
        try:
            with socket.create_connection(("127.0.0.1", 8080), timeout=1):
                break
        except OSError as error:
            if time.monotonic() >= deadline:
                raise RuntimeError("OpenWhisk proxy did not start") from error
            time.sleep(0.2)

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(
            "__main__.py",
            "import requests, sys\n"
            "def main(args):\n"
            "    return dict(echo=args['message'], python=list(sys.version_info[:2]), requests=requests.__version__)\n",
        )
    value = {"binary": True, "main": "main", "code": base64.b64encode(archive.getvalue()).decode("ascii")}
    request = urllib.request.Request(
        "http://127.0.0.1:8080/init", json.dumps({"value": value}).encode(), {"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.status == 200
    request = urllib.request.Request(
        "http://127.0.0.1:8080/run",
        json.dumps({"value": {"message": "multiarch"}}).encode(),
        {"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        assert json.load(response) == {"echo": "multiarch", "python": [3, 6], "requests": "2.19.1"}
    print("OpenWhisk ZIP action: init and run passed")
finally:
    server.terminate()
    try:
        server.wait(timeout=5)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait()

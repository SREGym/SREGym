#!/usr/bin/env python3
"""Request the trusted setup/runtime cutover through the private controller."""

from __future__ import annotations

import os
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import NoReturn


TOKEN_PATH = "/run/agent-egress-cutover/token"
CONTROLLER_URL = "http://agent-egress-controller:9191"


def fail(message: str) -> NoReturn:
    raise SystemExit(f"agent egress cutover: {message}")


def switch_phase(phase: str) -> None:
    token = open(TOKEN_PATH).read().strip()
    request = urllib.request.Request(
        f"{CONTROLLER_URL}/phase/{phase}",
        data=b"",
        method="POST",
        headers={"Authorization": f"Bearer {token}"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=135) as response:
            if response.status != 200:
                fail(f"controller returned HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        fail(f"controller rejected {phase} (HTTP {exc.code}): {exc.read().decode()}")
    except urllib.error.URLError as exc:
        fail(f"controller was unreachable for {phase}: {exc}")


def wait_for_phase(phase: str) -> None:
    expected = "200" if phase == "bootstrap" else "403"
    port = int(os.environ.get("AGENT_EGRESS_PROXY_PORT", "3128"))
    deadline = time.monotonic() + 30
    consecutive = 0
    last = "no response"
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(
                ("agent-egress-proxy", port), timeout=5
            ) as sock:
                sock.sendall(
                    b"CONNECT raw.githubusercontent.com:443 HTTP/1.1\r\n"
                    b"Host: raw.githubusercontent.com:443\r\n\r\n"
                )
                last = sock.recv(128).splitlines()[0].decode(errors="replace")
        except OSError as exc:
            last = str(exc)
            consecutive = 0
        else:
            status = last.split(" ", 2)[1] if " " in last else ""
            consecutive = consecutive + 1 if status == expected else 0
            if consecutive == 3:
                return
        time.sleep(1)
    fail(f"{phase} phase did not stabilize (last probe: {last})")


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in {"bootstrap", "runtime"}:
        fail("expected phase bootstrap or runtime")
    phase = sys.argv[1]
    if os.geteuid() != 0:
        fail("must run as root")
    if not os.path.isfile(TOKEN_PATH) or os.path.getsize(TOKEN_PATH) == 0:
        fail("controller credential is missing")
    if stat.S_IMODE(os.stat(TOKEN_PATH).st_mode) != 0o400:
        fail("controller credential mode is not root-only")
    unreadable = subprocess.run(
        ["runuser", "-u", "agent", "--", "test", "-r", TOKEN_PATH],
        check=False,
    )
    if unreadable.returncode == 0:
        fail("agent can read the cutover credential")

    switch_phase(phase)
    wait_for_phase(phase)
    print(f"agent egress cutover: {phase} policy active")


if __name__ == "__main__":
    main()

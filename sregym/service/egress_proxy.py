"""HTTPS-capable egress proxy with path-level domain deny list.

Uses ``mitmdump`` (from the ``mitmproxy`` package) as a subprocess so that
HTTPS requests can be intercepted and inspected at the URL-path level — not
just by hostname.  A small addon script is written to a temp file and loaded
by ``mitmdump``; it checks every request against a configurable deny list and
returns 403 for matches.

The proxy generates a CA certificate on first run and stores it in a
per-session temp directory.  The CA cert is **appended** to a copy of the
system CA bundle so that the agent container trusts both the proxy and all
real CAs (API providers, etc.).

Requires ``mitmdump`` to be installed — e.g. ``uv tool install mitmproxy``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import signal
import socket
import ssl
import subprocess
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

NO_PROXY = "localhost,127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,host.docker.internal,.svc,.cluster.local"

CA_BUNDLE_ENV_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS", "CURL_CA_BUNDLE")

DEFAULT_DENY_DOMAINS: list[str] = [
    "github.com/SREGym",
    "raw.githubusercontent.com/SREGym",
    "api.github.com/repos/SREGym",
    "codeload.github.com/SREGym",
]

# mitmproxy addon script loaded by mitmdump at runtime.
_ADDON_SCRIPT = '''\
"""mitmproxy addon — deny-list filter for SREGym egress proxy."""
import json
import os
from mitmproxy import http

DENY_LIST: list[str] = json.loads(os.environ.get("SREGYM_DENY_DOMAINS", "[]"))
PROXY_MODE: str = os.environ.get("SREGYM_PROXY_MODE", "filtered")
BLOCK_LOG_PATH: str = os.environ.get("SREGYM_BLOCK_LOG", "")


def _is_denied(host: str, path: str) -> bool:
    for entry in DENY_LIST:
        if "/" in entry:
            deny_host, deny_path = entry.split("/", 1)
            if host == deny_host and path.lstrip("/").startswith(deny_path):
                return True
        else:
            if host == entry:
                return True
    return False


def request(flow: http.HTTPFlow) -> None:
    host = flow.request.pretty_host
    path = flow.request.path

    if PROXY_MODE == "off" or _is_denied(host, path):
        msg = f"Blocked by SREGym egress policy: {host}{path}"
        flow.response = http.Response.make(403, msg, {"Content-Type": "text/plain"})
        if BLOCK_LOG_PATH:
            try:
                with open(BLOCK_LOG_PATH, "a") as f:
                    f.write(msg + "\\n")
            except OSError:
                pass
'''


def _find_mitmdump() -> str:
    """Locate the ``mitmdump`` binary, preferring uv-tool-managed installs.

    The uv-tool install (``~/.local/bin/mitmdump``) is checked first because
    it runs in its own isolated virtualenv, avoiding dependency conflicts with
    the project's pinned ``cryptography`` version.
    """
    # 1. Prefer uv tool install — isolated from project deps
    uv_tool_path = Path.home() / ".local" / "bin" / "mitmdump"
    if uv_tool_path.exists():
        return str(uv_tool_path)

    # 2. Fall back to PATH (may be a system or pipx install)
    path = shutil.which("mitmdump")
    if path:
        return path

    raise FileNotFoundError(
        "mitmdump not found — required for --internet-access filtered|off.\n"
        "Install it with:  uv tool install mitmproxy\n"
        "Or:               pip install mitmproxy"
    )


def _build_combined_ca_bundle(proxy_ca_cert: Path, output_path: Path) -> None:
    """Create a CA bundle containing the system CAs plus the proxy CA.

    This avoids replacing the system trust store (which would break connections
    to API providers like Anthropic, OpenAI, AWS Bedrock, etc.).

    Note: uses the *host's* system CAs, not the container's.  The standard
    Mozilla/CA root set is nearly identical across host and container, so this
    works in practice.  If the container image ships a stripped CA bundle, this
    may need revisiting.
    """
    # Get the system CA bundle path
    system_ca = ssl.get_default_verify_paths().cafile
    if system_ca and Path(system_ca).exists():
        system_certs = Path(system_ca).read_text()
    else:
        # Fall back to certifi if available, else empty
        try:
            import certifi

            system_certs = Path(certifi.where()).read_text()
        except ImportError:
            system_certs = ""

    proxy_cert = proxy_ca_cert.read_text()
    output_path.write_text(system_certs.rstrip("\n") + "\n" + proxy_cert)


def _pick_free_port() -> int:
    """Bind to port 0, record the OS-assigned port, then close the socket."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


class EgressProxy:
    """Host-side MITM proxy enforcing a domain deny list with path-level
    granularity for both HTTP and HTTPS.

    Uses ``mitmdump`` as a subprocess.  Generates a CA certificate that must
    be mounted into agent containers for HTTPS interception.

    Usage::

        proxy = EgressProxy(mode="filtered", deny_domains=[...])
        port = proxy.start()
        ca_bundle = proxy.ca_bundle_path  # mount this into the container
        # … run agent containers with http_proxy / https_proxy / SSL_CERT_FILE
        proxy.stop()
        print(proxy.blocked_requests())
    """

    def __init__(
        self,
        mode: str = "filtered",
        deny_domains: list[str] | None = None,
    ):
        if mode not in ("on", "off", "filtered"):
            raise ValueError(f"Invalid mode: {mode!r} — expected 'on', 'off', or 'filtered'")
        self.mode = mode
        self.deny_domains = deny_domains or list(DEFAULT_DENY_DOMAINS)
        self._proc: subprocess.Popen | None = None
        self._tmpdir: str | None = None
        self.port: int | None = None

    @property
    def ca_bundle_path(self) -> Path | None:
        """Path to the combined CA bundle (system CAs + proxy CA) for containers."""
        if self._tmpdir:
            return Path(self._tmpdir) / "sregym-ca-bundle.pem"
        return None

    def start(self) -> int:
        """Start the proxy on a free port and return the port number."""
        if self.mode == "on":
            raise RuntimeError("Proxy should not be started in 'on' mode")

        mitmdump = _find_mitmdump()
        self._tmpdir = tempfile.mkdtemp(prefix="sregym-egress-")
        tmpdir = Path(self._tmpdir)

        addon_path = tmpdir / "deny_addon.py"
        addon_path.write_text(_ADDON_SCRIPT)

        block_log = tmpdir / "blocked.log"

        env = os.environ.copy()
        env["SREGYM_DENY_DOMAINS"] = json.dumps(self.deny_domains)
        env["SREGYM_PROXY_MODE"] = self.mode
        env["SREGYM_BLOCK_LOG"] = str(block_log)

        self.port = _pick_free_port()
        cmd = [
            mitmdump,
            "-q",
            "--set",
            f"confdir={tmpdir}",
            "-p",
            str(self.port),
            "--set",
            "stream_large_bodies=1m",
            "-s",
            str(addon_path),
        ]

        self._proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
            text=True,
        )

        self._wait_for_ready(tmpdir)

        # Build combined CA bundle (system CAs + proxy CA) so that
        # SSL_CERT_FILE doesn't replace the system trust store.
        proxy_ca = tmpdir / "mitmproxy-ca-cert.pem"
        _build_combined_ca_bundle(proxy_ca, tmpdir / "sregym-ca-bundle.pem")

        logger.info(f"Egress proxy started on port {self.port} (mode={self.mode}, pid={self._proc.pid})")
        return self.port

    def stop(self):
        """Stop the proxy subprocess. Temp files are kept until cleanup()."""
        if self._proc:
            with contextlib.suppress(ProcessLookupError, OSError):
                os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
            self._proc.wait(timeout=5)
            self._proc = None
            logger.info("Egress proxy stopped")

    def cleanup(self):
        """Remove temp files (CA bundle, addon script, block log).

        Call this only after all agent containers have exited — the CA bundle
        is bind-mounted into containers, so removing it while they're running
        breaks their HTTPS trust.
        """
        if self._tmpdir and Path(self._tmpdir).exists():
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None

    def blocked_requests(self) -> list[str]:
        """Return the list of blocked requests (read from the log file)."""
        if not self._tmpdir:
            return []
        log_path = Path(self._tmpdir) / "blocked.log"
        if not log_path.exists():
            return []
        return [line.strip() for line in log_path.read_text().splitlines() if line.strip()]

    def _wait_for_ready(self, confdir: Path, timeout: float = 15) -> None:
        """Wait for mitmdump to start listening and generate its CA cert.

        Polls with a TCP connect to the configured port and checks for the
        CA certificate file on disk.
        """
        deadline = time.monotonic() + timeout
        ca_cert = confdir / "mitmproxy-ca-cert.pem"

        while time.monotonic() < deadline:
            if self._proc and self._proc.poll() is not None:
                remaining = self._proc.stderr.read() if self._proc.stderr else ""
                raise RuntimeError(f"mitmdump exited with code {self._proc.returncode}: {remaining}")

            port_up = False
            with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                port_up = True

            if port_up and ca_cert.exists():
                return

            time.sleep(0.2)

        raise TimeoutError(f"Egress proxy did not start within {timeout}s")

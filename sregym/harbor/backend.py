"""SREGym backend process for the ``sregym`` sidecar of a Harbor task.

The DinD entrypoint (``docker/dind/entrypoint.sh``) starts the private Docker
daemon and KIND cluster, then runs this module. It deploys the configured
problem, injects its fault and exposes SREGym's filtered Kubernetes API proxy
to the agent container. It then stays alive: several mitigation oracles compare
against a baseline captured in memory immediately before injection, so grading
must use this same problem object rather than a fresh process.

Interfaces (see ``sregym.harbor.protocol``):

- Shared volume: ``state``, ``status.json`` and the agent ``kubeconfig``.
- ``0.0.0.0:API_PORT``: ``GET /status``; ``POST /oracle/recover`` runs the
  problem's ``recover_fault()`` for Harbor's oracle agent. It requires a bearer
  token whose SHA-256 is configured on the sidecar; only the task's reference
  solution contains the token.
- ``127.0.0.1:GRADE_PORT``: ``POST /grade`` evaluates the mitigation oracle once
  and caches the verdict. Harbor calls it through a collect hook that runs
  inside this container after the agent container has been stopped.
"""

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import os
import shutil
import signal
import tempfile
import threading
import time
import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Protocol

from sregym.harbor import protocol, selftest

logger = logging.getLogger("all.sregym.harbor")

DEPLOY_ATTEMPTS = 2
RECOVERY_READY_TIMEOUT_S = 600


class ProblemSession(Protocol):
    """The problem lifecycle the backend drives. Replaced by a fake in tests."""

    def setup(self) -> str:
        """Deploy and inject the fault. Returns the agent kubeconfig path."""

    def grade(self) -> dict:
        """Evaluate the mitigation oracle against the current cluster state."""

    def recover(self) -> None:
        """Apply the problem's reference recovery."""

    def close(self) -> None:
        """Release local listeners before exit."""


class ConductorSession:
    """Runs one problem through SREGym's Conductor, as ``main.py`` does."""

    def __init__(self, problem_id: str, *, advertise_host: str, proxy_port: int):
        self.problem_id = problem_id
        self.advertise_host = advertise_host
        self.proxy_port = proxy_port
        self.conductor = None

    def setup(self) -> str:
        from sregym.conductor.conductor import Conductor, ConductorConfig
        from sregym.conductor.constants import StartProblemResult
        from sregym.service.internet_policy import InternetAccessMode, InternetPolicy
        from sregym.service.kubectl import ContainerPlatformError

        # Harbor controls the agent's network access; the cluster boundary used
        # by SREGym's filtered local runs does not apply to this agent.
        config = ConductorConfig(
            deploy_loki=False,
            internet_policy=InternetPolicy(mode=InternetAccessMode.OPEN),
            k8s_proxy_listen_host="0.0.0.0",
            k8s_proxy_listen_port=self.proxy_port,
            k8s_proxy_advertise_host=self.advertise_host,
            stages=("mitigation",),
        )
        self.conductor = Conductor(config=config)
        self.conductor.problem_id = self.problem_id

        for attempt in range(1, DEPLOY_ATTEMPTS + 1):
            try:
                result = asyncio.run(self.conductor.start_problem())
                break
            except ContainerPlatformError:
                raise
            except Exception:
                logger.exception(f"start_problem failed (attempt {attempt}/{DEPLOY_ATTEMPTS})")
                if attempt == DEPLOY_ATTEMPTS:
                    raise
                # start_problem() removes leftover app resources itself, but a
                # partially injected fault may also live outside the namespace.
                if self.conductor.fault_injected and self.conductor.problem is not None:
                    self.conductor.problem.recover_fault()
        if result == StartProblemResult.SKIPPED_KHAOS_REQUIRED:
            raise RuntimeError(f"Problem '{self.problem_id}' requires Khaos, which KIND clusters cannot run")

        self.conductor.start_k8s_proxy()
        return self.conductor.get_agent_kubeconfig_path()

    def grade(self) -> dict:
        return self.conductor._evaluate_mitigation("")

    def recover(self) -> None:
        problem = self.conductor.problem
        problem.recover_fault()
        namespaces = getattr(problem.app, "namespaces", None) or [problem.namespace]
        for namespace in namespaces:
            try:
                self.conductor.kubectl.wait_for_ready(namespace, max_wait=RECOVERY_READY_TIMEOUT_S)
            except Exception:
                # The grader waits for rollouts itself; this only avoids grading
                # the reference solution while its pods are still starting.
                logger.warning(f"Pods in {namespace} were not ready after recovery", exc_info=True)

    def close(self) -> None:
        if self.conductor is not None:
            self.conductor.stop_k8s_proxy()
            self.conductor.mcp_server.stop_port_forward()


def _write_atomic(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Backend:
    """Owns the shared state files, HTTP listeners and the single grade."""

    def __init__(
        self,
        session: ProblemSession,
        *,
        problem_id: str,
        shared_dir: Path,
        output_dir: Path,
        oracle_token_sha256: str | None,
    ):
        self.session = session
        self.problem_id = problem_id
        self.shared_dir = shared_dir
        self.output_dir = output_dir
        self.oracle_token_sha256 = (oracle_token_sha256 or "").strip().lower() or None
        self.state = protocol.STATE_STARTING
        self.error: str | None = None
        # Grading, recovery and setup all act on the live cluster; never overlap them.
        self._lock = threading.Lock()
        self._grade: dict | None = None
        self._servers: list[ThreadingHTTPServer] = []

    # -- state ---------------------------------------------------------------

    def set_state(self, state: str, error: str | None = None) -> None:
        self.state = state
        self.error = error
        # The agent can read these files: never include the problem ID.
        status = {"state": state, "updated_at": _now()}
        if error:
            status["error"] = error
        _write_atomic(self.shared_dir / protocol.STATUS_NAME, json.dumps(status) + "\n")
        _write_atomic(self.shared_dir / protocol.STATE_NAME, state + "\n")
        logger.info(f"[HARBOR] Backend state: {state}{f' ({error})' if error else ''}")

    def setup(self) -> bool:
        self.set_state(protocol.STATE_DEPLOYING)
        try:
            with self._lock:
                kubeconfig = self.session.setup()
            # Readable by any agent user; the file only grants the proxy's
            # filtered view of the cluster.
            shutil.copyfile(kubeconfig, self.shared_dir / protocol.KUBECONFIG_NAME)
            os.chmod(self.shared_dir / protocol.KUBECONFIG_NAME, 0o644)
        except BaseException as exc:
            logger.exception("[HARBOR] Problem setup failed")
            self.set_state(protocol.STATE_FAILED, f"{type(exc).__name__}: {exc}")
            if not isinstance(exc, Exception):
                raise
            return False
        self.set_state(protocol.STATE_READY)
        return True

    # -- operations ----------------------------------------------------------

    def grade(self) -> dict:
        with self._lock:
            if self._grade is not None:
                return self._grade
            if self.state != protocol.STATE_READY:
                return {
                    "problem_id": self.problem_id,
                    "success": False,
                    "error": f"backend is {self.state}: {self.error or 'problem was not set up'}",
                    "graded_at": _now(),
                }
            started = time.monotonic()
            try:
                verdict = self.session.grade()
            except Exception as exc:
                logger.exception("[HARBOR] Mitigation grading raised")
                verdict = {"success": False, "error": f"{type(exc).__name__}: {exc}"}
            self._grade = {
                "problem_id": self.problem_id,
                "success": verdict.get("success") is True,
                "mitigation": verdict,
                "graded_at": _now(),
                "duration_s": round(time.monotonic() - started, 3),
            }
            grade_path = self.output_dir / Path(protocol.GRADE_PATH).name
            _write_atomic(grade_path, json.dumps(self._grade, indent=2, default=str) + "\n")
            logger.info(f"[HARBOR] Mitigation verdict: {self._grade['success']}")
            return self._grade

    def recover(self) -> dict:
        with self._lock:
            if self.state != protocol.STATE_READY:
                raise RuntimeError(f"backend is {self.state}")
            if self._grade is not None:
                raise RuntimeError("the problem has already been graded")
            started = time.monotonic()
            self.session.recover()
            return {"recovered": True, "duration_s": round(time.monotonic() - started, 3)}

    def token_is_valid(self, authorization: str | None) -> bool:
        if not self.oracle_token_sha256 or not authorization:
            return False
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return False
        digest = hashlib.sha256(token.strip().encode()).hexdigest()
        return hmac.compare_digest(digest, self.oracle_token_sha256)

    # -- HTTP ----------------------------------------------------------------

    def _handler(self, routes: dict[tuple[str, str], Callable[[BaseHTTPRequestHandler], tuple[int, dict]]]):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                logger.debug(f"[HARBOR] {self.address_string()} {format % args}")

            def _dispatch(self, method: str):
                route = routes.get((method, self.path.split("?", 1)[0]))
                if route is None:
                    status, body = HTTPStatus.NOT_FOUND, {"error": "not found"}
                else:
                    try:
                        status, body = route(self)
                    except Exception as exc:
                        logger.exception(f"[HARBOR] {method} {self.path} failed")
                        status, body = HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"{type(exc).__name__}: {exc}"}
                payload = (json.dumps(body, default=str) + "\n").encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                self._dispatch("GET")

            def do_POST(self):
                self._dispatch("POST")

        return Handler

    def _status(self, _request) -> tuple[int, dict]:
        body = {"state": self.state}
        if self.error:
            body["error"] = self.error
        return HTTPStatus.OK, body

    def _oracle_recover(self, request) -> tuple[int, dict]:
        if not self.token_is_valid(request.headers.get("Authorization")):
            return HTTPStatus.FORBIDDEN, {"error": "oracle recovery requires the task's solution token"}
        try:
            return HTTPStatus.OK, self.recover()
        except RuntimeError as exc:
            return HTTPStatus.CONFLICT, {"error": str(exc)}

    def _grade_route(self, _request) -> tuple[int, dict]:
        return HTTPStatus.OK, self.grade()

    def start_servers(self, *, api_host: str, api_port: int, grade_port: int) -> None:
        public = {("GET", "/status"): self._status, ("POST", "/oracle/recover"): self._oracle_recover}
        local = {("GET", "/status"): self._status, ("POST", "/grade"): self._grade_route}
        for host, port, routes in ((api_host, api_port, public), ("127.0.0.1", grade_port, local)):
            server = ThreadingHTTPServer((host, port), self._handler(routes))
            server.daemon_threads = True
            threading.Thread(target=server.serve_forever, name=f"harbor-api-{port}", daemon=True).start()
            self._servers.append(server)
            logger.info(f"[HARBOR] Listening on {host}:{server.server_address[1]}")

    def stop_servers(self) -> None:
        for server in self._servers:
            server.shutdown()
            server.server_close()
        self._servers.clear()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--problem", default=os.environ.get(protocol.PROBLEM_ID_ENV))
    parser.add_argument("--advertise-host", default=protocol.SERVICE_NAME)
    parser.add_argument("--api-port", type=int, default=protocol.API_PORT)
    parser.add_argument("--grade-port", type=int, default=protocol.GRADE_PORT)
    parser.add_argument("--proxy-port", type=int, default=protocol.K8S_PROXY_PORT)
    parser.add_argument("--shared-dir", type=Path, default=Path(protocol.BACKEND_SHARED_DIR))
    parser.add_argument("--output-dir", type=Path, default=Path(protocol.BACKEND_OUTPUT_DIR))
    args = parser.parse_args(argv)
    if not args.problem:
        parser.error(f"--problem or {protocol.PROBLEM_ID_ENV} is required")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.shared_dir.mkdir(parents=True, exist_ok=True)
    # init_logger() writes its file under AGENT_LOGS_DIR.
    os.environ.setdefault("AGENT_LOGS_DIR", protocol.LOG_DIR)
    os.environ.setdefault("MCP_SERVER_PORT", "9954")
    os.environ["MCP_SERVER_URL"] = f"http://127.0.0.1:{os.environ['MCP_SERVER_PORT']}"

    from logger import init_logger

    init_logger()

    stop = threading.Event()

    def request_stop(signum, _frame):
        stop.set()
        # Deployment can take many minutes; do not make Compose wait it out.
        if backend.state != protocol.STATE_READY:
            raise SystemExit(128 + signum)

    if args.problem == selftest.PROBLEM_ID:
        session = selftest.SelfTestSession(advertise_host=args.advertise_host, proxy_port=args.proxy_port)
    else:
        session = ConductorSession(args.problem, advertise_host=args.advertise_host, proxy_port=args.proxy_port)
    backend = Backend(
        session,
        problem_id=args.problem,
        shared_dir=args.shared_dir,
        output_dir=args.output_dir,
        oracle_token_sha256=os.environ.get(protocol.ORACLE_TOKEN_SHA256_ENV),
    )
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, request_stop)
    backend.set_state(protocol.STATE_STARTING)
    backend.start_servers(api_host="0.0.0.0", api_port=args.api_port, grade_port=args.grade_port)
    try:
        # Setup failures keep the process alive: the agent container reports
        # the failed state, and Harbor can still collect logs from this service.
        backend.setup()
        stop.wait()
    except Exception:
        traceback.print_exc()
        raise
    finally:
        backend.stop_servers()
        try:
            session.close()
        except Exception:
            logger.warning("[HARBOR] Backend cleanup failed", exc_info=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

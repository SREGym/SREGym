import asyncio
import logging
import os
import threading

import pyfiglet
from fastapi import FastAPI, HTTPException
from fastmcp import FastMCP
from fastmcp.server.http import create_sse_app
from pydantic import BaseModel
from rich.markdown import Markdown
from rich.panel import Panel
from starlette.routing import Mount
from uvicorn import Config, Server

from logger import console
from sregym.conductor.submission import (
    SUBMISSION_STAGE_ORDER,
    SUBMISSION_STAGES,
    EvaluationInProgress,
    SubmissionAttemptClosed,
    SubmissionAttemptMismatch,
    SubmissionStage,
    SubmissionStageMismatch,
)

_conductor = None

submit_mcp = FastMCP("Submit MCP Server")

_SUBMISSION_WAIT_SECONDS = 300.0
_SUBMISSION_POLL_SECONDS = 0.1


class SubmissionRequestRejected(RuntimeError):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(message)


async def _submit_when_stage_is_ready(solution: str, stage: str | None) -> dict:
    conductor = _conductor
    if conductor is None:
        raise SubmissionRequestRejected(400, "No problem has been started")

    try:
        requested_stage, generation = conductor.register_submission_request(solution, stage)
    except ValueError as exc:
        raise SubmissionRequestRejected(400, str(exc)) from exc
    except SubmissionAttemptClosed as exc:
        raise SubmissionRequestRejected(409, str(exc)) from exc
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _SUBMISSION_WAIT_SECONDS

        while True:
            current_stage, evaluating, current_generation = conductor.submission_state()

            if current_generation != generation:
                raise SubmissionRequestRejected(409, "This submission belongs to an earlier benchmark attempt.")

            if current_stage == "done":
                raise SubmissionRequestRejected(409, "This benchmark attempt is already closed.")

            if current_stage in {"setup", None}:
                raise SubmissionRequestRejected(
                    409,
                    f"Cannot submit before an agent stage is ready; current stage is {current_stage!r}.",
                )

            if current_stage == "tearing_down":
                if loop.time() >= deadline:
                    raise SubmissionRequestRejected(
                        503,
                        f"Stage {requested_stage!r} did not become available before the submission timeout.",
                    )
                await asyncio.sleep(_SUBMISSION_POLL_SECONDS)
                continue

            if current_stage not in SUBMISSION_STAGES:
                raise SubmissionRequestRejected(409, f"Cannot submit at stage: {current_stage!r}")

            current_order = SUBMISSION_STAGE_ORDER[current_stage]
            requested_order = SUBMISSION_STAGE_ORDER[requested_stage]
            if current_order > requested_order:
                raise SubmissionRequestRejected(
                    409,
                    f"Stage {requested_stage!r} has already passed; current stage is {current_stage!r}.",
                )

            if current_order < requested_order:
                if not evaluating:
                    raise SubmissionRequestRejected(
                        409,
                        f"Stage {requested_stage!r} is not ready; submit stage {current_stage!r} first.",
                    )
                if loop.time() >= deadline:
                    raise SubmissionRequestRejected(
                        503,
                        f"Stage {requested_stage!r} did not become available before the submission timeout.",
                    )
                await asyncio.sleep(_SUBMISSION_POLL_SECONDS)
                continue

            try:
                result = await conductor.submit(
                    solution,
                    expected_stage=requested_stage,
                    expected_generation=generation,
                )
            except EvaluationInProgress as exc:
                raise SubmissionRequestRejected(409, str(exc)) from exc
            except SubmissionStageMismatch:
                if loop.time() >= deadline:
                    raise SubmissionRequestRejected(
                        503,
                        f"Stage {requested_stage!r} did not become available before the submission timeout.",
                    ) from None
                await asyncio.sleep(_SUBMISSION_POLL_SECONDS)
                continue
            except (SubmissionAttemptClosed, SubmissionAttemptMismatch) as exc:
                raise SubmissionRequestRejected(409, str(exc)) from exc
            except RuntimeError as exc:
                if loop.time() >= deadline:
                    raise SubmissionRequestRejected(503, str(exc)) from exc
                await asyncio.sleep(_SUBMISSION_POLL_SECONDS)
                continue

            if result.get("status") != "accepted" or result.get("stage") != requested_stage:
                raise SubmissionRequestRejected(
                    409,
                    f"Stage {requested_stage!r} did not accept the submission.",
                )
            return result
    finally:
        conductor.unregister_pending_submission(requested_stage, generation)


@submit_mcp.tool(name="submit")
async def submit_via_conductor(ans: str, stage: str | None = None) -> dict[str, str]:
    """Submit task result to benchmark

    Args:
        ans (str): task result that the agent submits

    Returns:
        dict[str]: acknowledgment of submission status
    """
    try:
        result = await _submit_when_stage_is_ready(ans, stage)
    except SubmissionRequestRejected as exc:
        return {"status": "error", "text": str(exc)}
    except Exception as exc:
        return {"status": "error", "text": f"Grading error: {exc}"}

    return {"status": "200", "text": result["message"], "stage": result["stage"]}


app = FastAPI(
    routes=[
        Mount("/submit_mcp", app=create_sse_app(submit_mcp, "/messages/", "/sse")),
    ]
)

_server: Server | None = None
_shutdown_event = threading.Event()

logger = logging.getLogger("all.sregym.conductor_api")


class _ShutdownNoiseFilter(logging.Filter):
    """Suppress expected CancelledError tracebacks from uvicorn during shutdown."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Case 1: exc_info carries the exception object directly.
        if record.exc_info and record.exc_info[1] is not None:
            import asyncio

            if isinstance(record.exc_info[1], asyncio.CancelledError):
                return False
        # Case 2: uvicorn formats the traceback as a plain string message
        # (e.g. logger.error(traceback.format_exc())) with no exc_info.
        # The string will end with "asyncio.exceptions.CancelledError".
        return "CancelledError" not in record.getMessage()


def request_shutdown():
    """
    Signal the API server to shut down.
    Safe to call from any thread and idempotent.
    """
    logger.warning("Shutting down API server...")

    # Suppress expected CancelledError noise from uvicorn tearing down
    # long-lived SSE connections during shutdown
    for name in ("uvicorn.error", "uvicorn"):
        logging.getLogger(name).addFilter(_ShutdownNoiseFilter())

    _shutdown_event.set()
    if _server is not None:
        # force_exit skips waiting for long-lived connections (like MCP SSE)
        # to close gracefully — the agent is already cleaned up at this point
        _server.force_exit = True
        _server.should_exit = True


def set_conductor(c):
    """Inject the shared Conductor instance."""
    global _conductor
    _conductor = c


class SubmitRequest(BaseModel):
    solution: str
    stage: SubmissionStage | None = None


@app.post("/submit")
async def submit_solution(req: SubmitRequest):
    try:
        result = await _submit_when_stage_is_ready(req.solution, req.stage)
    except SubmissionRequestRejected as exc:
        logger.error("Submission rejected: %s", exc)
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("Grading error: %s", exc)
        raise HTTPException(status_code=400, detail=f"Grading error: {exc}") from exc

    return {"status": "200", "message": result["message"], "stage": result["stage"]}


@app.get("/status")
async def get_status():
    conductor = _conductor
    if conductor is None:
        logger.error("No problem has been started")
        raise HTTPException(status_code=400, detail="No problem has been started")
    stage, _, _ = conductor.submission_state()
    logger.debug(f"API returns Current stage: {stage}")
    return {"stage": stage}


@app.get("/get_app")
async def get_app():
    if _conductor is None:
        logger.error("No problem has been started")
        raise HTTPException(status_code=400, detail="No problem has been started")
    app_inst = _conductor.app
    logger.debug(f"API returns App instance: {app_inst}")
    namespaces = getattr(app_inst, "namespaces", None) or [app_inst.namespace]
    return {
        "app_name": app_inst.app_name,
        "namespace": app_inst.namespace,
        "namespaces": namespaces,
        "descriptions": str(app_inst.description),
    }


def run_api(conductor):
    """
    Start the API server and block until request_shutdown() is called.
    """
    global _server
    set_conductor(conductor)
    logger.debug(f"API server is binded to the conductor {conductor}")

    # Load from .env with defaults
    host = os.getenv("API_BIND_HOST", "0.0.0.0")
    port = int(os.getenv("API_PORT", "8000"))

    logger.debug(f"API server starting on http://{host}:{port}")

    art = pyfiglet.figlet_format("SREGym")
    console.print(Panel(art, title="SREGym API Server", subtitle=f"http://{host}:{port}", style="bold green"))
    console.print(
        Markdown(
            """
**Available Endpoints**
- **POST /submit**: `{ "stage": "diagnosis", "solution": "<your-solution>" }` → grades the named stage
- **GET /status**: returns `{ "stage": "setup" | "diagnosis" | "mitigation" | "tearing_down" | "done" }`
"""
        )
    )

    config = Config(
        app=app,
        host=host,
        port=port,
        log_level="info",
        timeout_graceful_shutdown=5,
        # log_config=None: don't install uvicorn's default StreamHandlers, which
        # capture sys.stderr at construction time and would tear through the
        # benchmark progress bar's live region. Falls back to root logger,
        # which our RichHandler owns.
        log_config=None,
    )
    config.install_signal_handlers = False
    server = Server(config)
    _server = server  # expose to request_shutdown()

    # watcher thread: when _shutdown_event is set, flip server.should_exit
    def _watch():
        _shutdown_event.wait()
        logger.debug("API server shutdown event received")
        server.should_exit = True

    threading.Thread(target=_watch, name="api-shutdown-watcher", daemon=True).start()

    try:
        logger.debug("API server is running")
        server.run()  # blocks until should_exit becomes True
    finally:
        # cleanup for potential reuse
        _shutdown_event.clear()
        _server = None

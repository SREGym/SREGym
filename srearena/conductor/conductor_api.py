import os
import threading
from typing import Optional

import pyfiglet
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from uvicorn import Config, Server

app = FastAPI()
_conductor = None

_server: Optional[Server] = None
_shutdown_event = threading.Event()


def request_shutdown():
    """
    Signal the API server to shut down.
    Safe to call from any thread and idempotent.
    """
    _shutdown_event.set()
    if _server is not None:
        _server.should_exit = True


def set_conductor(c):
    """Inject the shared Conductor instance."""
    global _conductor
    _conductor = c


class SubmitRequest(BaseModel):
    solution: str


@app.post("/submit")
async def submit_solution(req: SubmitRequest):
    allowed = {"noop", "detection", "localization", "mitigation"}
    if _conductor is None or _conductor.submission_stage not in allowed:
        raise HTTPException(status_code=400, detail=f"Cannot submit at stage: {_conductor.submission_stage!r}")

    wrapped = f'```\nsubmit("{req.solution}")\n```'
    try:
        results = await _conductor.submit(wrapped)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Grading error: {e}")

    return results


@app.get("/status")
async def get_status():
    if _conductor is None:
        raise HTTPException(status_code=400, detail="No problem has been started")
    stage = _conductor.submission_stage
    return {"stage": stage}


@app.get("/get_app")
async def get_app():
    if _conductor is None:
        raise HTTPException(status_code=400, detail="No problem has been started")
    app_inst = _conductor.app
    return {"app_name": app_inst.app_name, "namespace": app_inst.namespace, "descriptions": str(app_inst.description)}


@app.get("/get_problem")
async def get_problem():
    if _conductor is None:
        raise HTTPException(status_code=400, detail="No problem has been started")
    problem_id = _conductor.problem_id
    return {"problem_id": problem_id}


def run_api(conductor):
    """
    Start the API server and block until request_shutdown() is called.
    """
    global _server
    set_conductor(conductor)

    # Load from .env with defaults
    host = os.getenv("API_HOSTNAME", "0.0.0.0")
    port = int(os.getenv("API_PORT", "8000"))

    console = Console()
    art = pyfiglet.figlet_format("SREArena")
    console.print(Panel(art, title="SREArena API Server", subtitle=f"http://{host}:{port}", style="bold green"))
    console.print(
        Markdown(
            """
**Available Endpoints**
- **POST /submit**: `{ "solution": "<your-solution>" }` → grades the current stage  
- **GET /status**: returns `{ "stage": "setup" | "noop" | "detection" | "localization" | "mitigation" | "done" }`
"""
        )
    )

    config = Config(app=app, host=host, port=port, log_level="info")
    config.install_signal_handlers = False
    server = Server(config)
    _server = server  # expose to request_shutdown()

    # watcher thread: when _shutdown_event is set, flip server.should_exit
    def _watch():
        _shutdown_event.wait()
        server.should_exit = True

    threading.Thread(target=_watch, name="api-shutdown-watcher", daemon=True).start()

    try:
        server.run()  # blocks until should_exit becomes True
    finally:
        # cleanup for potential reuse
        _shutdown_event.clear()
        _server = None

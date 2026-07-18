"""
OpenSRE agent driver for SREGym.

Orchestrates a planner -> executor pipeline, entirely on the SREGym side:

  DIAGNOSIS stage  -> OpenSRE investigates the cluster and produces a root cause;
                      the driver submits that text (graded by the LLM judge).
  MITIGATION stage -> a pluggable SREGym coding agent (the "executor") applies
                      OpenSRE's remediation plan via kubectl; the driver submits
                      an empty solution to trigger the live-cluster oracle.

OpenSRE is never modified: it is invoked as the installed ``opensre`` CLI. Only
the mitigation executor touches the cluster, because OpenSRE's own Kubernetes
integration is read-only.

Config via env:
  OPENSRE_EXECUTOR        which SREGym agent applies the fix (default: claudecode)
  OPENSRE_DIAGNOSIS_ONLY  "1" to skip mitigation (submit empty) — good for a spike
  AGENT_MODEL_ID          model for the executor (and informational for OpenSRE)
  AGENT_LOGS_DIR          logs directory
  API_HOSTNAME/API_PORT   conductor API location
"""

import argparse
import importlib
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

from clients.harness.problem_id import resolve_problem_id
from clients.opensre.opensre_agent import OpenSREAgent
from logger import init_logger

# Add SREGym root to path (mirrors the other client drivers).
sregym_root = Path(__file__).resolve().parents[2]
if str(sregym_root) not in sys.path:
    sys.path.insert(0, str(sregym_root))

init_logger()
logger = logging.getLogger("all.opensre.driver")


# Pluggable mitigation executors — any SREGym agent exposing the shared
# `__init__(logs_dir, model_name)` + `run(instruction) -> int` interface works.
EXECUTORS: dict[str, tuple[str, str]] = {
    "claudecode": ("clients.claudecode.claudecode_agent", "ClaudeCodeAgent"),
    "codex": ("clients.codex.codex_agent", "CodexAgent"),
    "opencode": ("clients.opencode.opencode_agent", "OpenCodeAgent"),
    "geminicli": ("clients.geminicli.geminicli_agent", "GeminiCliAgent"),
    "copilot": ("clients.copilot.copilot_agent", "CopilotCliAgent"),
}


# --------------------------------------------------------------------------- #
# Conductor API helpers
# --------------------------------------------------------------------------- #
def get_api_base_url() -> str:
    host = os.getenv("API_HOSTNAME", "localhost")
    port = os.getenv("API_PORT", "8000")
    return f"http://{host}:{port}"


def get_app_info() -> dict:
    resp = requests.get(f"{get_api_base_url()}/get_app")
    resp.raise_for_status()
    return resp.json()


def get_stage() -> str | None:
    try:
        resp = requests.get(f"{get_api_base_url()}/status")
        resp.raise_for_status()
        return resp.json().get("stage")
    except requests.RequestException as exc:
        logger.debug(f"status check failed: {exc}")
        return None


def wait_for_stage(target: str, timeout: int = 600) -> None:
    """Block until the conductor reports ``target`` stage, or raise TimeoutError."""
    start = time.time()
    logger.info(f"Waiting for conductor stage: {target}")
    while time.time() - start < timeout:
        stage = get_stage()
        if stage == target:
            logger.info(f"Conductor reached stage: {target}")
            return
        if stage == "done":
            raise RuntimeError(f"Conductor reached 'done' before expected stage '{target}'")
        time.sleep(2)
    raise TimeoutError(f"Conductor did not reach stage '{target}' within {timeout}s")


def submit(solution: str) -> None:
    """POST a solution to the conductor for the current stage."""
    resp = requests.post(f"{get_api_base_url()}/submit", json={"solution": solution})
    logger.info(f"Submitted (len={len(solution)}): {resp.status_code} {resp.text[:200]}")


# --------------------------------------------------------------------------- #
# Prompt / incident construction
# --------------------------------------------------------------------------- #
def _namespace_block(app_info: dict) -> str:
    namespaces = app_info.get("namespaces") or [app_info.get("namespace", "default")]
    if len(namespaces) > 1:
        return f"namespaces {', '.join(namespaces)} (this scenario spans multiple namespaces)"
    return f"namespace {namespaces[0]}"


def build_incident(app_info: dict) -> str:
    """The alert message handed to OpenSRE for diagnosis."""
    app_name = app_info.get("app_name", "unknown")
    descriptions = app_info.get("descriptions", "")
    return (
        f"A fault has been injected into the Kubernetes application '{app_name}' "
        f"running in {_namespace_block(app_info)}. Investigate the live cluster and "
        f"its observability data to determine the root cause of the failure.\n\n"
        f"{descriptions}"
    )


def build_executor_instruction(app_info: dict, plan: str) -> str:
    """The prompt handed to the mitigation executor.

    It carries OpenSRE's diagnosis so the executor acts as OpenSRE's "hands"
    rather than re-diagnosing from scratch. The driver — not the executor —
    owns /submit, so we explicitly tell the executor not to submit.
    """
    app_name = app_info.get("app_name", "unknown")
    namespaces = app_info.get("namespaces") or [app_info.get("namespace", "default")]
    return f"""You are an SRE agent applying a fix to a Kubernetes application.

Application: {app_name}
Namespace(s): {", ".join(namespaces)}

A diagnosis has already been produced by an upstream investigation agent:

{plan}

YOUR TASK: Apply the remediation to the live cluster using kubectl. Work
autonomously — do NOT ask for confirmation. You have kubectl access to the
namespace(s) above.

IMPORTANT:
- Implement the fix that resolves the root cause described above.
- If the diagnosis is incomplete, investigate as needed and fix the real issue.
- Do NOT submit anything to any API and do NOT call any /submit endpoint.
  Simply apply the fix with kubectl and then exit. The harness handles submission.
"""


# --------------------------------------------------------------------------- #
# Executor loading
# --------------------------------------------------------------------------- #
def load_executor(name: str, logs_dir: Path, model_name: str):
    """Instantiate a mitigation executor by name from the EXECUTORS registry."""
    if name not in EXECUTORS:
        raise ValueError(f"Unknown executor '{name}'. Choose one of: {', '.join(sorted(EXECUTORS))}")
    module_path, class_name = EXECUTORS[name]
    module = importlib.import_module(module_path)
    agent_class = getattr(module, class_name)
    logger.info(f"Loaded mitigation executor: {name} ({class_name})")
    return agent_class(logs_dir=logs_dir / f"executor_{name}", model_name=model_name)


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #
def save_results(logs_dir: Path, problem_id: str, usage_metrics: dict, executor: str) -> None:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    results_file = logs_dir / f"opensre_results_{problem_id}_{timestamp}.json"
    results_file.write_text(
        json.dumps(
            {
                "problem_id": problem_id,
                "timestamp": timestamp,
                "planner": "opensre",
                "executor": executor,
                "usage_metrics": usage_metrics,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    logger.info(f"Saved results to {results_file}")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(description="Run the OpenSRE planner + SREGym executor on a SREGym task")
    parser.add_argument("--model", default=os.getenv("AGENT_MODEL_ID", "claude-sonnet-4-5"))
    parser.add_argument("--logs-dir", default=os.environ.get("AGENT_LOGS_DIR", "./logs/opensre"))
    parser.add_argument("--problem-id", default=None)
    parser.add_argument(
        "--executor",
        default=os.getenv("OPENSRE_EXECUTOR", "claudecode"),
        help=f"Mitigation executor: {', '.join(sorted(EXECUTORS))}",
    )
    parser.add_argument(
        "--diagnosis-only",
        action="store_true",
        default=os.getenv("OPENSRE_DIAGNOSIS_ONLY") == "1",
        help="Skip mitigation (submit empty). Useful for a first plumbing spike.",
    )
    args = parser.parse_args()

    logs_dir = Path(args.logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)
    problem_id = resolve_problem_id(cli_problem_id=args.problem_id)

    logger.info("=" * 80)
    logger.info("Starting OpenSRE planner for SREGym")
    logger.info(f"Executor: {args.executor} | diagnosis_only={args.diagnosis_only} | model={args.model}")
    logger.info("=" * 80)

    # OpenSRE must be installed on PATH (from the official package), never the clone.
    OpenSREAgent.ensure_installed()

    # ---- DIAGNOSIS STAGE ------------------------------------------------- #
    wait_for_stage("diagnosis")
    app_info = get_app_info()
    incident = build_incident(app_info)

    planner = OpenSREAgent(logs_dir=logs_dir / "planner", model_name=args.model)
    result = planner.investigate(incident, title=f"SREGym incident: {app_info.get('app_name', 'app')}")
    diagnosis = planner.diagnosis_text(result)
    logger.info(f"OpenSRE diagnosis:\n{diagnosis}")
    submit(diagnosis)

    # ---- MITIGATION STAGE ------------------------------------------------ #
    wait_for_stage("mitigation")
    if args.diagnosis_only:
        logger.info("diagnosis-only mode: submitting empty mitigation (expected to fail the oracle)")
        submit("")
    else:
        executor = load_executor(args.executor, logs_dir, args.model)
        instruction = build_executor_instruction(app_info, planner.remediation_plan(result))
        logger.info("Running mitigation executor to apply OpenSRE's plan...")
        rc = executor.run(instruction)
        logger.info(f"Executor finished with return code {rc}")
        submit("")  # driver owns submission; triggers the live-cluster oracle

    save_results(logs_dir, problem_id, planner.get_usage_metrics(), args.executor)
    logger.info("OpenSRE driver run complete.")


if __name__ == "__main__":
    main()

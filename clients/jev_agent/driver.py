"""jev_agent driver for SREGym (diagnosis only).

Flow: wait for the conductor's diagnosis stage -> read app info -> start delay -> collect the cluster snapshot
-> build the overview and AgentContext -> Investigator (a decision model picks one generic evidence tool per
step) -> report (evidence selection + write-up) -> submit.

Artifacts under --logs-dir with prefix ``jev_agent_<problem>_<stamp>``: ``_trace.jsonl`` (decision trace),
``_kubectl_raw.jsonl.gz`` (raw kubectl recording), ``_tools.jsonl.gz`` (tool cache), ``_snapshot.json``,
``_jev_requests.json``, ``_diagnosis.txt``, ``_results.json``, and a trajectory under ``trajectory/``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

sregym_root = Path(__file__).resolve().parents[2]
if str(sregym_root) not in sys.path:
    sys.path.insert(0, str(sregym_root))

from logger import init_logger  # noqa: E402

init_logger()

from clients.harness.problem_id import resolve_problem_id  # noqa: E402
from clients.harness.token_usage import usage_metrics  # noqa: E402
from clients.jev_agent import config as agent_config  # noqa: E402
from clients.jev_agent import llm as llm_mod  # noqa: E402
from clients.jev_agent.notebook import Notebook, overview_hints  # noqa: E402
from clients.jev_agent.policy import DecisionAsker, Investigator  # noqa: E402
from clients.jev_agent.report import build_report, results_payload  # noqa: E402
from clients.jev_agent.types import AgentContext  # noqa: E402
from clients.jev_diag import decisions_client  # noqa: E402
from clients.jev_diag import trace as trace_mod  # noqa: E402
from clients.jev_diag.classifier import make_client  # noqa: E402
from clients.jev_diag.collector import (  # noqa: E402
    RawRecorder,
    collect_snapshot,
    fetch_alerts_via_mcp,
    set_kubectl_observer,
    set_kubectl_recorder,
)
from clients.jev_diag.config import jev_model  # noqa: E402
from clients.jev_diag.driver import collection_summary, get_app_info, submit_diagnosis, wait_for_stage  # noqa: E402
from clients.jev_diag.driver import run_preflight as jev_diag_preflight  # noqa: E402

AGENT_NAME = agent_config.AGENT_NAME
logger = logging.getLogger("all.jev_agent.driver")


class AgentTrace(trace_mod.DecisionTrace):
    """jev_diag's DecisionTrace, with the trajectory named and labelled for jev_agent."""

    def __init__(self, path: Path, *, problem_id: str, run_args: dict | None = None) -> None:
        """Open the trace file and record the agent name."""
        super().__init__(path, problem_id=problem_id, run_args=run_args)
        self.record("run", "agent", agent=AGENT_NAME)

    def write_trajectory(self, out_dir: Path, *, stage: str = "diagnosis") -> Path:
        """Write the recorded steps as an SREGym agent trajectory file under `out_dir` and return its path."""
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%m%d_%H%M")
        path = out_dir / f"{stamp}_{self.problem_id}_{AGENT_NAME}_agent_trajectory.jsonl"
        trace_mod.write_trajectory(self.records, path, problem_id=self.problem_id, stage=stage)
        lines = path.read_text(encoding="utf-8").splitlines()
        if lines:
            meta = json.loads(lines[0])
            meta["agent"] = AGENT_NAME
            lines[0] = json.dumps(meta, ensure_ascii=False)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path


def run_preflight() -> None:
    """Decisions backend check (fatal), MCP reachability (warning only), LLM ping when an LLM is configured."""
    jev_diag_preflight()
    try:
        from clients.jev_agent import mcp

        ok = mcp.preflight(timeout=10)
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"MCP preflight error: {exc}")
    print("MCP reachable" if ok else "WARNING: MCP (Prometheus) unreachable; metric tools will be disabled")
    model = os.environ.get(llm_mod.MODEL_ENV, "").strip()
    if model:
        try:
            resp = llm_mod.ping(model)
        except Exception as exc:  # noqa: BLE001 - report and exit non-zero
            print(f"LLM check failed for {model}: {exc}")
            sys.exit(1)
        print(f"LLM reachable; using {resp.model}")


def write_json(path: Path, payload: object) -> None:
    """Write `payload` to `path` as indented JSON."""
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    """Command-line options (defaults from the environment and the agent config)."""
    cfg = agent_config.load()
    parser = argparse.ArgumentParser(description="Diagnosis-only SRE agent: decision-model tool loop (jev_agent)")
    parser.add_argument("--logs-dir", default=os.environ.get("AGENT_LOGS_DIR", "./logs/jev_agent"))
    parser.add_argument("--problem-id", default=None, help="Artifact id (default: SREGYM_ARTIFACT_ID)")
    parser.add_argument("--namespace", action="append", default=None, help="Standalone mode: namespace(s) to inspect")
    parser.add_argument("--app-name", default=None, help="Standalone mode: application name")
    parser.add_argument("--no-submit", action="store_true", help="Run the investigation but do not submit")
    parser.add_argument("--no-alerts", action="store_true", help="Skip the Prometheus MCP alert lookup")
    parser.add_argument("--no-raw", action="store_true", help="Do not record raw kubectl output")
    parser.add_argument("--replay-tools", default=None, help="Replay tool results from an earlier _tools.jsonl.gz")
    parser.add_argument("--max-steps", type=int, default=None, help=f"tool step budget (default {cfg.max_steps})")
    parser.add_argument("--policy", choices=("model", "scripted"), default=None)
    parser.add_argument(
        "--start-delay",
        type=float,
        default=cfg.start_delay,
        help="seconds to wait after the conductor hands over the application (env JEV_AGENT_START_DELAY, default "
        "120; not applied in standalone mode)",
    )
    parser.add_argument("--wait-timeout", type=int, default=600, help="Seconds to wait for the diagnosis stage")
    return parser.parse_args()


def main() -> None:
    """Entry point: run one diagnosis, submit it, and write the results, trace and trajectory files."""
    args = parse_args()
    logs_dir = Path(args.logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)
    problem_id = resolve_problem_id(cli_problem_id=args.problem_id)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = logs_dir / f"{AGENT_NAME}_{problem_id}_{stamp}"
    started = time.time()
    cfg = agent_config.load()
    if args.max_steps is not None:
        cfg.max_steps = max(1, args.max_steps)
    if args.policy:
        cfg.policy = args.policy

    trace = AgentTrace(
        prefix.with_name(prefix.name + "_trace.jsonl"),
        problem_id=problem_id,
        run_args={
            **vars(args),
            "agent_config": cfg.to_dict(),
            "typesafe_model": jev_model() or "jev-latest",
            "decision_backend": os.getenv(decisions_client.BACKEND_ENV) or "typesafe",
            "decisions_model": os.getenv(decisions_client.MODEL_ENV) or decisions_client.DEFAULT_MODEL,
        },
    )
    set_kubectl_observer(trace.kubectl_observer())
    recorder = None
    if not args.no_raw:
        recorder = RawRecorder(prefix.with_name(prefix.name + "_kubectl_raw.jsonl.gz"))
        set_kubectl_recorder(recorder)
    status = "error"
    results: dict = {
        "agent": AGENT_NAME,
        "problem_id": problem_id,
        "timestamp": stamp,
        "success": False,
        "trace_path": str(trace.path),
    }
    try:
        status = run(args, cfg, trace, prefix, results, started)
    except SystemExit:
        status = "exit"
        raise
    except BaseException as exc:
        trace.record("run", "error", error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc(limit=12))
        logger.error("jev_agent failed: %s", exc)
        raise
    finally:
        set_kubectl_observer(None)
        set_kubectl_recorder(None)
        if recorder is not None:
            recorder.close()
        try:
            trajectory = trace.write_trajectory(logs_dir / "trajectory")
            results["trajectory_path"] = str(trajectory)
        except Exception as exc:  # noqa: BLE001 - never let trace rendering mask the run outcome
            logger.warning("Could not write trajectory: %s", exc)
        trace.close(status, duration_seconds=round(time.time() - started, 1))
        write_json(prefix.with_name(prefix.name + "_results.json"), results)


def _span_label(trace) -> str | None:
    """The span-metric label that names services (detected through Prometheus, traced), or None when detection fails."""
    from clients.jev_agent import mcp, metrics

    def call(query: str) -> str:
        return mcp.call_mcp("prometheus", "get_metrics", {"query": query})

    try:
        with trace.timed("setup", "span_label") as info:
            label = metrics.detect_span_label(call)
            info["span_label"] = label
        return label
    except Exception as exc:  # noqa: BLE001 - metric tools fall back to unfiltered queries
        logger.warning("span label detection failed: %s", exc)
        return None


EARLY_EXPLORE_GRACE_S = 240.0  # how long to wait past the start delay for an unfinished early explore


class _EarlyExplore:
    """discovery.propose in a background thread during the start delay, on a snapshot taken at its start.

    Its kubectl reads stay out of the raw recording (a replay must serve the real collection's reads). The main
    thread only sleeps meanwhile and joins before reading the cluster itself, so the trace has one writer at a
    time.
    """

    def __init__(self, app_info: dict, namespaces: list[str], llm, cfg, tools_mod) -> None:
        """Start exploring in a background thread."""
        self.info: dict = {}
        self._proposal = None
        self._thread = threading.Thread(
            target=self._run, args=(app_info, namespaces, llm, cfg, tools_mod), name="early-explore", daemon=True
        )
        self._thread.start()

    def _run(self, app_info, namespaces, llm, cfg, tools_mod) -> None:
        """Snapshot the cluster (no logs, not recorded) and propose tools for it; the outcome and duration go to
        `info`."""
        from clients.jev_agent import discovery
        from clients.jev_diag import collector

        t0 = time.monotonic()
        try:
            with collector.recording_paused():
                snapshot = collect_snapshot(app_info, alerts_fetcher=None, include_logs=False)
                ctx = AgentContext(app_info=app_info, namespaces=namespaces, snapshot=snapshot, overview={})
                self._proposal = discovery.propose(llm, ctx, tools_mod.REGISTRY, cfg.cache_dir or None)
            self.info.update(cache=self._proposal.cache, specs=len(self._proposal.specs), error=self._proposal.error)
        except Exception as exc:  # noqa: BLE001 - exploring then happens after the delay as before
            logger.warning("early explore failed: %s", exc, exc_info=True)
            self.info["error"] = f"{type(exc).__name__}: {exc}"
        self.info["seconds"] = round(time.monotonic() - t0, 1)

    def result(self, grace_s: float):
        """The proposal once the thread finishes within `grace_s` seconds, else None (noted in `info`)."""
        self._thread.join(grace_s)
        if self._thread.is_alive():
            self.info["error"] = f"still running {grace_s:.0f}s after the delay"
            return None
        return self._proposal


@dataclass
class _Session:
    """The investigation's inputs built after the start delay, plus the setup values the results report."""

    snapshot: Any
    overview: dict
    scores: dict
    ctx: AgentContext
    cache: Any
    notebook: Notebook
    mcp_ok: bool
    span_label: str | None


def run(args, cfg, trace, prefix: Path, results: dict, started: float) -> str:
    """Wait for the stage, read the cluster, investigate, write and submit the report; returns the run status."""
    from clients.jev_agent import overview as overview_mod
    from clients.jev_agent import tools as tools_mod

    standalone = bool(args.namespace)
    app_info = _read_app_info(args, trace, standalone)
    if app_info is None:
        return "skipped_stage"
    namespaces = [ns for ns in (app_info.get("namespaces") or [app_info.get("namespace")]) if ns]
    logger.info("Diagnosing %s in namespaces %s", app_info.get("app_name"), namespaces)

    delay = 0.0 if standalone else max(0.0, args.start_delay)
    llm = llm_mod.make_llm(cfg.llm_model) if cfg.llm_model and cfg.max_llm_calls > 0 else None
    proposal = _wait_and_explore(trace, cfg, app_info, namespaces, llm, tools_mod, delay)
    session = _collect_session(args, cfg, trace, prefix, app_info, namespaces, tools_mod, overview_mod)
    if cfg.craft and llm is not None:
        _adopt_crafted_tools(trace, cfg, llm, session, tools_mod, proposal)
    asker = DecisionAsker(make_client(), trace)
    inv, report = _investigate_and_report(trace, cfg, llm, asker, session, prefix, tools_mod, overview_mod)
    text = report.text
    (prefix.with_name(prefix.name + "_diagnosis.txt")).write_text(text, encoding="utf-8")
    logger.info("Diagnosis:\n%s", text)

    submission = _submit(args, trace, results, text, standalone)
    results.update(results_payload(inv, report, asker, llm, submission_text=text))
    results.update(
        {
            "duration_seconds": round(time.time() - started, 1),
            "success": True,
            "mcp_ok": session.mcp_ok,
            "span_label": session.span_label,
            "collection_errors": session.snapshot.errors,
            "submission": submission,
            "usage_metrics": usage_metrics(input_tokens=asker.input_tokens, output_tokens=asker.output_tokens),
        }
    )
    return "ok" if submission is not None or args.no_submit or standalone else "submit_failed"


def _read_app_info(args, trace, standalone: bool) -> dict | None:
    """The application to diagnose, from the command line or the conductor; None when the conductor is not at the
    diagnosis stage."""
    if standalone:
        app_info = {
            "app_name": args.app_name or args.namespace[0],
            "namespace": args.namespace[0],
            "namespaces": args.namespace,
            "descriptions": "",
        }
        trace.record("setup", "app_info", source="cli", app_info=app_info)
        return app_info
    with trace.timed("setup", "wait_for_stage") as info:
        stage = wait_for_stage(args.wait_timeout)
        info["stage"] = stage
    if stage != "diagnosis":
        logger.warning("Conductor is at stage %r; this agent only handles diagnosis. Exiting.", stage)
        return None
    with trace.timed("setup", "get_app"):
        app_info = get_app_info()
    trace.record("setup", "app_info", source="conductor", app_info=app_info)
    return app_info


def _wait_and_explore(trace, cfg, app_info: dict, namespaces: list[str], llm, tools_mod, delay: float):
    """Sleep the start delay, exploring the environment meanwhile when crafting is on; the explore proposal or None."""
    # Exploring (inventory + the LLM's tool specs) does not need the fault to have surfaced: it runs during the
    # start delay. Validating and smoke-testing the specs waits for the delay like every other read.
    early = (
        _EarlyExplore(app_info, namespaces, llm, cfg, tools_mod) if cfg.craft and llm is not None and delay else None
    )
    if delay > 0:
        logger.info("Waiting %.0fs before reading the cluster so the fault has time to surface", delay)
        with trace.timed("setup", "start_delay", seconds=delay):
            time.sleep(delay)
    if early is None:
        return None
    with trace.timed("setup", "explore_propose") as info:
        proposal = early.result(EARLY_EXPLORE_GRACE_S)
        info.update(early.info)
    return proposal


def _collect_session(args, cfg, trace, prefix: Path, app_info: dict, namespaces: list[str], tools_mod, overview_mod):
    """Collect and save the snapshot, build the overview, check MCP, and assemble the context and notebook."""
    with trace.timed("collect", "snapshot"):
        snapshot = collect_snapshot(app_info, alerts_fetcher=None if args.no_alerts else fetch_alerts_via_mcp)
    trace.record("collect", "summary", **collection_summary(snapshot))
    write_json(
        prefix.with_name(prefix.name + "_snapshot.json"),
        {**snapshot.to_state(include_private=True), "collection_errors": snapshot.errors},
    )

    with trace.timed("setup", "overview") as info:
        overview = overview_mod.build_overview(snapshot)
        scores = overview_mod.component_scores(overview)
        rendered = overview_mod.render(overview, budget_tokens=cfg.overview_budget)
        info["scores"] = scores
    from clients.jev_agent import mcp

    with trace.timed("setup", "mcp_preflight") as info:
        mcp_ok = bool(mcp.preflight(timeout=10))
        info["mcp_ok"] = mcp_ok
    span_label = _span_label(trace) if mcp_ok else None
    cache = tools_mod.ToolCache(
        Path(args.replay_tools) if args.replay_tools else prefix.with_name(prefix.name + "_tools.jsonl.gz"),
        replay=bool(args.replay_tools),
    )
    ctx = AgentContext(
        app_info=app_info,
        namespaces=namespaces,
        snapshot=snapshot,
        overview=overview,
        mcp_ok=mcp_ok,
        span_label=span_label,
        cache=cache,
        extra={"change_window_s": cfg.change_window_s},
    )
    notebook = Notebook(
        app_info,
        rendered,
        scores=scores,
        rerender=lambda b: overview_mod.render(overview, budget_tokens=b),
        hints=overview_hints(overview),
    )
    return _Session(snapshot, overview, scores, ctx, cache, notebook, mcp_ok, span_label)


def _adopt_crafted_tools(trace, cfg, llm, session: _Session, tools_mod, proposal) -> None:
    """Validate and smoke-test the LLM's crafted tools into the registry (exploring now if no proposal is ready),
    note the smoke tests as step-0 observations, and give the explore and CRAFT calls their own LLM budget."""
    from clients.jev_agent import discovery

    ctx, scores, notebook = session.ctx, session.scores, session.notebook
    with trace.timed("setup", "explore") as info:
        try:
            if proposal is not None:
                explored = discovery.adopt(
                    llm, ctx, tools_mod.REGISTRY, scores, proposal, cache_dir=cfg.cache_dir or None
                )
            else:
                explored = discovery.explore(llm, ctx, tools_mod.REGISTRY, scores, cache_dir=cfg.cache_dir or None)
            info.update(
                kept=explored.kept,
                dropped=explored.dropped,
                error=explored.error,
                usage=explored.usage,
                raw_text=explored.raw_text,
                cache=explored.cache,
                inventory_sizes={k: len(v) for k, v in explored.inventory.items() if isinstance(v, list | dict)},
            )
            logger.info("explore: kept %d crafted tool(s), dropped %d", len(explored.kept), len(explored.dropped))
            # the smoke tests already observed the environment: they open the notebook (step 0)
            for name, target, trial in explored.trials:
                notebook.add(0, name, target, trial)
        except Exception as exc:  # noqa: BLE001 - the investigation runs with the fixed menu instead
            logger.warning("explore failed, continuing with the fixed tool menu: %s", exc, exc_info=True)
            info["error"] = f"{type(exc).__name__}: {exc}"
    # the explore call and the CRAFT choices have their own budget: escalation and the report keep theirs
    cfg.max_llm_calls += getattr(llm, "calls", 0) + cfg.max_craft * (1 + discovery.CRAFT_RETRIES)


def _investigate_and_report(trace, cfg, llm, asker, session: _Session, prefix: Path, tools_mod, overview_mod):
    """Run the investigation, review its origin, and build the report; always save the decision requests and
    close the tool cache. Returns (investigation, report)."""
    ctx, notebook, scores = session.ctx, session.notebook, session.scores
    try:
        investigator = Investigator(
            ctx, asker, notebook, trace, llm, config=cfg, tools=tools_mod, overview=overview_mod, scores=scores
        )
        inv = investigator.run()
        if llm is not None and cfg.review:
            _review_origin(trace, cfg, llm, ctx, notebook, inv)
        elif llm is not None:
            trace.record("report", "origin_review_skipped", reason="JEV_AGENT_REVIEW=0")
        with trace.timed("report", "build") as info:
            report = build_report(
                asker,
                notebook,
                inv,
                ctx,
                llm=llm,
                max_llm_calls=cfg.max_llm_calls,
                scores=scores,
                overview_mod=overview_mod,
            )
            info["source"] = report.source
            info["evidence"] = report.evidence
            info["llm_error"] = report.llm_error
    finally:
        write_json(prefix.with_name(prefix.name + "_jev_requests.json"), asker.requests)
        try:
            session.cache.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("tool cache close failed: %s", exc)
    return inv, report


def _review_origin(trace, cfg, llm, ctx: AgentContext, notebook: Notebook, inv) -> None:
    """Let the LLM review the investigation's origin, replacing it when the review names another, and keep the
    review's mechanism for the report."""
    from clients.jev_agent import review as review_mod

    cfg.max_llm_calls += 1  # the review has its own call; escalation and the report keep theirs
    with trace.timed("report", "origin_review") as info:
        rev = review_mod.review_origin(llm, ctx, notebook, inv)
        info.update(
            before=rev.before,
            after=rev.after,
            reason=rev.reason,
            mechanism=rev.mechanism,
            candidates=rev.candidates,
            error=rev.error,
            raw_text=rev.raw_text,
        )
    if rev.after and rev.after != inv.origin:
        logger.info("origin review: %s -> %s (%s)", inv.origin, rev.after, rev.reason)
        inv.origin = rev.after
    if rev.mechanism:
        ctx.extra["origin_review"] = {"reason": rev.reason, "mechanism": rev.mechanism}


def _submit(args, trace, results: dict, text: str, standalone: bool):
    """Submit the diagnosis to the conductor unless told not to; the conductor's response, or None."""
    submission = None
    if args.no_submit or standalone:
        trace.record("submit", "skipped", reason="--no-submit" if args.no_submit else "standalone")
        return submission
    try:
        with trace.timed("submit", "response") as info:
            submission = submit_diagnosis(text)
            info["response"] = submission
    except Exception as exc:  # noqa: BLE001 - the diagnosis is still worth recording
        logger.error("Submission failed: %s", exc)
        results["submission_error"] = str(exc)
    return submission


if __name__ == "__main__":
    main()

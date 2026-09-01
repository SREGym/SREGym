"""SREGym-side AURA agent: HTTP client + per-problem orchestration.

Gets copied verbatim from this repo into
``<MEZMO_BENCH_SREGYM_ROOT>/clients/aura/aura_agent.py`` by
``mezmo-bench sregym-run`` before it invokes SREGym. The SREGym venv on the
operator's host imports this as ``clients.aura.aura_agent``.

Stdlib + ``httpx`` only — must NOT import from ``mezmo_benchmark.*`` or
``benchmarks.*``, because the SREGym venv does not install those
packages. The mezmo-side orchestrator (``benchmarks/sregym/orchestrator.py``)
handles AURA's spawn/shutdown lifecycle separately; this module only
talks HTTP to an already-running AURA instance.

Preflight is called once per driver invocation (not per problem). Run is
called once per (problem, attempt) pair.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Local import — system_message_builder ships alongside this file (see T045's
# preflight copy step). Constitution Principle II enforcement boundary.
from .system_message_builder import build_sregym_system_message

# Chat-completions URL path. Matches the AURA OpenAI-compatible server.
_AURA_CHAT_COMPLETIONS_PATH = "/v1/chat/completions"

# Provider-credential env-var allowlist. Preflight passes if any one is set —
# the active credentials depend on which `[llm].provider` the operator's TOML
# selected. Mirrors the discipline in
# ``mezmo_benchmark.adapters.aura._build_aura_env``.
_PROVIDER_CRED_VARS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_PROFILE",
)

# SREGym MCP env vars. The mezmo-side orchestrator spawns fastmcp bridges
# for the active kubectl/jaeger/prometheus tool surface. The bridge URLs are
# injected as SREGYM_BRIDGE_*_URL env vars and resolved into AURA's TOML at
# config load. See docs/sregym.md for why stdio + uvx mcp-proxy didn't work
# (AURA rmcp 0.12 closes the stdio child after handshake).
_REQUIRED_SREGYM_ENV_VARS: tuple[str, ...] = (
    "SREGYM_BRIDGE_KUBECTL_URL",
    "SREGYM_BRIDGE_JAEGER_URL",
    "SREGYM_BRIDGE_PROMETHEUS_URL",
)

# FR-015: exponential-backoff retry on transient AURA failures.
# Delay sequence between attempts 1→2, 2→3, 3→4: total overhead <= 7s.
_RETRY_DELAYS_SECONDS: tuple[float, ...] = (1.0, 2.0, 4.0)
_RETRY_HTTP_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})


class AuraAgentPreflightError(RuntimeError):
    """Raised when AuraAgent.preflight detects a missing prerequisite."""


@dataclass(frozen=True)
class AuraRunResult:
    """Per-problem AURA invocation result.

    The ``usage`` and ``tool_counts`` dicts are produced by helpers in
    ``mezmo_benchmark.adapters.aura`` and merged into the SREGym-side
    result.json on the mezmo-bench side (orchestrator). The SREGym-side
    driver just persists ``transcript_path`` and ``answer`` so the
    extractor has data to read.
    """

    answer: str
    process_status: str
    duration_seconds: float
    transcript_path: Path
    usage: dict[str, Any] = field(default_factory=dict)
    tool_counts: dict[str, Any] = field(default_factory=dict)


class AuraAgent:
    """HTTP client driving AURA's ``/v1/chat/completions`` per SREGym problem.

    Does NOT spawn AURA — that's the orchestrator's job (see
    ``benchmarks/sregym/orchestrator.py``). The agent expects AURA to be
    listening on ``http://127.0.0.1:<aura_port>`` before ``run()`` is called.
    """

    def __init__(
        self,
        *,
        model_id: str,
        aura_port: int,
        transcript_dir: Path,
        aura_config_path: Path | None = None,
        aura_bin: Path | str | None = None,
        agent_version: str | None = None,
        sregym_root: Path | None = None,
        sregym_commit_sha: str | None = None,
        request_timeout_seconds: float = 900.0,
    ) -> None:
        self.model_id = model_id
        self.aura_port = aura_port
        self.transcript_dir = transcript_dir
        self.aura_config_path = aura_config_path
        self.aura_bin = aura_bin
        self.agent_version = agent_version
        self.sregym_root = sregym_root
        self.sregym_commit_sha = sregym_commit_sha
        self.request_timeout_seconds = request_timeout_seconds

    # ------------------------------------------------------------------
    # Preflight
    # ------------------------------------------------------------------

    def preflight(self) -> None:
        """Verify every prerequisite before the SREGym run starts.

        Order matters: cheap synchronous checks first (env vars, file
        existence), then subprocess-bound checks (AURA version, git
        rev-parse), then the I/O-bound HTTP probe last. A common
        configuration mistake surfaces in milliseconds rather than
        after a 5-second TCP timeout.

        Raises :class:`AuraAgentPreflightError` on the first failure
        with a message naming exactly what's missing.
        """
        self._check_aura_config()
        self._check_provider_credentials()
        self._check_sregym_env_vars()
        self._check_aura_version()
        self._check_sregym_commit_sha()
        self._check_pricing_snapshot_covers_model()
        self._check_aura_binary()  # HTTP probe — last (slowest)

    def _check_aura_binary(self) -> None:
        """Verify AURA is reachable at the configured host:port.

        Refactored 2026-05-19: the driver runs inside SREGym's venv and
        the AURA binary itself is host-spawned by the orchestrator (see
        ``benchmarks/sregym/orchestrator.py::_spawn_aura_for_sregym``).
        Checking ``shutil.which("aura-web-server")`` inside the driver is
        wrong — what matters is whether the host AURA is accepting HTTP
        on the agreed port.

        We probe ``/health`` and ``/v1/models`` (AURA's standard
        readiness endpoints). If the orchestrator passed an explicit
        ``aura_bin`` Path, we additionally verify the file exists — that
        catches orchestrator-side misconfiguration without forcing
        the driver to invoke the binary.
        """
        if self.aura_bin is not None:
            if not Path(self.aura_bin).is_file():
                raise AuraAgentPreflightError(
                    f"MEZMO_BENCH_AURA_BIN points at {self.aura_bin} which "
                    f"does not exist"
                )

        import urllib.error
        import urllib.request
        url = f"http://127.0.0.1:{self.aura_port}/health"
        try:
            with urllib.request.urlopen(url, timeout=5.0) as resp:  # nosec B310 - localhost
                if resp.status != 200:
                    raise AuraAgentPreflightError(
                        f"AURA /health at {url} returned HTTP {resp.status}"
                    )
        except (urllib.error.URLError, OSError) as exc:
            raise AuraAgentPreflightError(
                f"AURA not reachable at {url}: {exc}. "
                f"The mezmo-side orchestrator should spawn aura-web-server "
                f"before invoking SREGym's main.py."
            ) from exc

    def _check_aura_config(self) -> None:
        if self.aura_config_path is None:
            # Orchestrator-managed AURA config; agent has no config-path
            # responsibility. Skip.
            return
        if not Path(self.aura_config_path).is_file():
            raise AuraAgentPreflightError(
                f"AURA TOML missing: {self.aura_config_path}"
            )

    def _check_provider_credentials(self) -> None:
        present = [name for name in _PROVIDER_CRED_VARS if os.environ.get(name)]
        if not present:
            raise AuraAgentPreflightError(
                "no upstream provider credential found — set one of "
                f"{list(_PROVIDER_CRED_VARS)}"
            )

    def _check_sregym_env_vars(self) -> None:
        missing = [name for name in _REQUIRED_SREGYM_ENV_VARS if not os.environ.get(name)]
        if missing:
            raise AuraAgentPreflightError(
                f"SREGym bridge URLs not injected: {missing}. "
                "Run through `mezmo-bench sregym-run` so the orchestrator starts "
                "the local bridges and injects SREGYM_BRIDGE_*_URL."
            )

    def _check_aura_version(self) -> None:
        if self.agent_version is None:
            return
        try:
            cmd = [str(self.aura_bin) if self.aura_bin else "aura-web-server", "--version"]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=True,
            )
        except (FileNotFoundError, subprocess.SubprocessError) as exc:
            raise AuraAgentPreflightError(
                f"could not run `aura-web-server --version`: {exc}"
            ) from exc
        installed = (result.stdout or result.stderr).strip()
        if self.agent_version not in installed:
            raise AuraAgentPreflightError(
                f"AURA version mismatch: installed={installed!r} "
                f"pinned={self.agent_version!r}"
            )

    def _check_pricing_snapshot_covers_model(self) -> None:
        """FR-005 + US4: refuse to start if [llm].model is missing from the pricing snapshot.

        Reads ``config/aura-pricing.json`` (location override via
        ``MEZMO_BENCH_AURA_PRICING_PATH``) and confirms the upstream
        model id from the rendered TOML lives there. Without this check
        the cost extractor would emit ``cost_source = "unknown"`` for
        every cell — the operator would only notice at submission time.

        Resolution order for the model id:

        1. Parse ``self.aura_config_path`` (preferred — the rendered TOML).
        2. Fall back to ``model_id`` constructor arg (the OpenAI alias);
           if that doesn't look like an upstream model id we skip the
           check rather than false-fail.
        """
        if self.aura_config_path is None or not Path(self.aura_config_path).is_file():
            return
        import tomllib as _tomllib
        try:
            parsed = _tomllib.loads(
                Path(self.aura_config_path).read_text(encoding="utf-8", errors="strict")
            )
        except _tomllib.TOMLDecodeError:
            return  # validate_sregym_aura_toml handles parse errors elsewhere
        # v1.20.0+ schema: [agent.llm], not top-level [llm].
        agent_block = parsed.get("agent") if isinstance(parsed, dict) else None
        llm = agent_block.get("llm") if isinstance(agent_block, dict) else None
        if not isinstance(llm, dict):
            return
        upstream_model = llm.get("model")
        if not isinstance(upstream_model, str) or not upstream_model:
            return

        # Resolve the pricing snapshot path.
        pricing_path_str = os.environ.get("MEZMO_BENCH_AURA_PRICING_PATH")
        if pricing_path_str:
            pricing_path = Path(pricing_path_str)
        else:
            # Default: <repo root>/config/aura-pricing.json. The aura_agent
            # gets COPIED into <SREGYM_ROOT>/clients/aura/ at preflight, but
            # the orchestrator that drives preflight before the copy still
            # has the mezmo-bench checkout's config dir on disk. The env-
            # override makes the test path explicit.
            pricing_path = Path(__file__).resolve().parents[3] / "config" / "aura-pricing.json"

        if not pricing_path.is_file():
            raise AuraAgentPreflightError(
                f"AURA pricing snapshot missing at {pricing_path}. "
                f"Run `python scripts/refresh-pricing.py` to populate it."
            )
        try:
            payload = json.loads(pricing_path.read_text(encoding="utf-8", errors="strict"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AuraAgentPreflightError(
                f"AURA pricing snapshot at {pricing_path} failed to parse: {exc}"
            ) from exc
        models = payload.get("models", {}) if isinstance(payload, dict) else {}
        if not isinstance(models, dict) or upstream_model not in models:
            raise AuraAgentPreflightError(
                f"upstream model {upstream_model!r} not in pricing snapshot "
                f"({pricing_path}). Run `python scripts/refresh-pricing.py` to "
                f"extend the snapshot."
            )

    def _check_sregym_commit_sha(self) -> None:
        if not self.sregym_root or not self.sregym_commit_sha:
            return
        if self.sregym_commit_sha.strip().upper() == "TBD":
            # Pin not yet locked — early-development convenience; do not
            # block, but no real check happens either.
            return
        try:
            result = subprocess.run(
                ["git", "-C", str(self.sregym_root), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=True,
            )
        except (FileNotFoundError, subprocess.SubprocessError) as exc:
            raise AuraAgentPreflightError(
                f"could not read SREGym HEAD at {self.sregym_root}: {exc}"
            ) from exc
        head = result.stdout.strip()
        if not head.startswith(self.sregym_commit_sha) and head != self.sregym_commit_sha:
            raise AuraAgentPreflightError(
                f"SREGym checkout HEAD mismatch: "
                f"installed={head!r} pinned={self.sregym_commit_sha!r}. "
                f"Run `git -C {self.sregym_root} checkout {self.sregym_commit_sha}`."
            )

    # ------------------------------------------------------------------
    # Per-problem run
    # ------------------------------------------------------------------

    def run(
        self,
        instruction: str,
        *,
        problem_id: str,
        namespace: str,
        submission_endpoint: str,
        window_hint: str | None = None,
    ) -> AuraRunResult:
        """Drive AURA against a single SREGym problem.

        ``instruction`` is the verbatim Conductor instruction — passed to
        AURA as the ``user`` message body byte-for-byte. Constitution
        Principle II is enforced HERE: any future contributor tempted to
        wrap the instruction must answer to T020's byte-equality test.

        ``problem_id`` / ``namespace`` / ``submission_endpoint`` /
        ``window_hint`` flow into the ``system`` message via the pure
        builder.
        """
        # Lazy import: httpx is a runtime dep, but kept lazy so test files
        # can monkeypatch the HTTP layer without paying the import cost.
        import httpx

        # The system message lists MCP server NAMES; the actual Streamable
        # HTTP bridge URLs live in AURA's TOML and are resolved at spawn.
        system_text = build_sregym_system_message(
            problem_id=problem_id,
            namespace=namespace,
            submission_endpoint=submission_endpoint,
            window_hint=window_hint,
        )

        self.transcript_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = self.transcript_dir / "transcript.jsonl"
        answer_path = self.transcript_dir / "answer.txt"

        url = f"http://127.0.0.1:{self.aura_port}{_AURA_CHAT_COMPLETIONS_PATH}"
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        body: dict[str, Any] = {
            "model": self.model_id,
            "messages": [
                {"role": "system", "content": system_text},
                {"role": "user", "content": instruction},
            ],
            "stream": True,
        }

        answer_parts: list[str] = []
        process_status = "ok"
        started_monotonic = time.monotonic()

        with transcript_path.open("w", encoding="utf-8", errors="strict") as transcript:
            transcript.write(json.dumps({
                "timestamp": _now_iso_utc(),
                "event_type": "request",
                "method": "POST",
                "url": url,
                "body": body,
            }) + "\n")

            # FR-015: up to 4 attempts (initial + 3 retries) with
            # exponential backoff between attempts on retryable failures
            # (HTTP 429/5xx or httpx.HTTPError/TimeoutException). After
            # the final retry, the last failure's process_status sticks
            # and falls through to cost_source="parse_error" via the
            # usage extractor's no-usage-event path.
            max_attempts = 1 + len(_RETRY_DELAYS_SECONDS)
            for attempt in range(1, max_attempts + 1):
                answer_parts.clear()  # only keep the last attempt's content
                process_status = "ok"
                retryable_failure = False
                try:
                    with httpx.Client(
                        timeout=httpx.Timeout(self.request_timeout_seconds)
                    ) as client:
                        with client.stream(
                            "POST", url, json=body, headers=headers
                        ) as response:
                            if response.status_code != 200:
                                try:
                                    preview = response.read().decode(
                                        "utf-8", errors="replace"
                                    )[:2048]
                                except Exception:
                                    preview = ""
                                transcript.write(json.dumps({
                                    "timestamp": _now_iso_utc(),
                                    "event_type": "http_error",
                                    "status_code": response.status_code,
                                    "attempt": attempt,
                                    "body_preview": preview,
                                }) + "\n")
                                process_status = "http_error"
                                if response.status_code in _RETRY_HTTP_STATUS_CODES:
                                    retryable_failure = True
                            else:
                                for event in _parse_aura_sse(response.iter_bytes()):
                                    transcript.write(json.dumps({
                                        "timestamp": _now_iso_utc(),
                                        **event,
                                    }) + "\n")
                                    if event["event_type"] != "sse_chunk":
                                        continue
                                    decoded = event.get("decoded") or {}
                                    choices = (
                                        decoded.get("choices")
                                        if isinstance(decoded, dict)
                                        else None
                                    )
                                    if isinstance(choices, list) and choices:
                                        first = choices[0]
                                        if isinstance(first, dict):
                                            delta = first.get("delta") or {}
                                            if isinstance(delta, dict):
                                                piece = delta.get("content")
                                                if isinstance(piece, str):
                                                    answer_parts.append(piece)
                                    usage_obj = (
                                        decoded.get("usage")
                                        if isinstance(decoded, dict)
                                        else None
                                    )
                                    if isinstance(usage_obj, dict):
                                        transcript.write(json.dumps({
                                            "timestamp": _now_iso_utc(),
                                            "event_type": "usage",
                                            "raw": usage_obj,
                                        }) + "\n")
                except httpx.TimeoutException as exc:
                    transcript.write(json.dumps({
                        "timestamp": _now_iso_utc(),
                        "event_type": "http_error",
                        "status_code": 0,
                        "attempt": attempt,
                        "body_preview": f"timeout: {exc}",
                    }) + "\n")
                    process_status = "timeout"
                    retryable_failure = True
                except httpx.HTTPError as exc:
                    transcript.write(json.dumps({
                        "timestamp": _now_iso_utc(),
                        "event_type": "http_error",
                        "status_code": 0,
                        "attempt": attempt,
                        "body_preview": str(exc)[:2048],
                    }) + "\n")
                    process_status = "aura_unavailable"
                    retryable_failure = True

                if process_status == "ok":
                    break
                if not retryable_failure or attempt >= max_attempts:
                    break
                delay = _RETRY_DELAYS_SECONDS[attempt - 1]
                transcript.write(json.dumps({
                    "timestamp": _now_iso_utc(),
                    "event_type": "retry_scheduled",
                    "attempt": attempt,
                    "next_attempt_in_seconds": delay,
                }) + "\n")
                time.sleep(delay)

        answer = "".join(answer_parts)
        answer_path.write_text(answer, encoding="utf-8", errors="strict")
        return AuraRunResult(
            answer=answer,
            process_status=process_status,
            duration_seconds=round(time.monotonic() - started_monotonic, 3),
            transcript_path=transcript_path,
        )


# ----------------------------------------------------------------------
# Helpers (inline copies of SSE-parse + UTC stamp from the mezmo-side
# adapter — duplicated deliberately so this file is stdlib+httpx only).
# ----------------------------------------------------------------------

_DATA_PREFIX = b"data:"


def _parse_aura_sse(byte_iter):
    """Inline SSE parser — same contract as
    ``mezmo_benchmark.adapters.aura._parse_aura_sse`` but local so this
    file has zero mezmo-bench imports (it gets copied to
    ``<SREGYM_ROOT>/clients/aura/`` at preflight)."""
    buffer = b""
    for chunk in byte_iter:
        if not chunk:
            continue
        buffer += chunk
        while b"\n\n" in buffer:
            event_bytes, _, rest = buffer.partition(b"\n\n")
            buffer = rest
            yield from _decode_sse_event(event_bytes)
    if buffer.strip():
        yield from _decode_sse_event(buffer)


def _decode_sse_event(event_bytes: bytes):
    for line in event_bytes.splitlines():
        line = line.strip()
        if not line.startswith(_DATA_PREFIX):
            continue
        payload = line[len(_DATA_PREFIX):].strip()
        if not payload:
            continue
        if payload == b"[DONE]":
            yield {"event_type": "done"}
            continue
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            yield {
                "event_type": "parse_error",
                "raw": payload.decode("utf-8", errors="replace"),
                "reason": str(exc),
            }
            continue
        yield {
            "event_type": "sse_chunk",
            "raw": payload.decode("utf-8", errors="replace"),
            "decoded": decoded,
        }


def _now_iso_utc() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

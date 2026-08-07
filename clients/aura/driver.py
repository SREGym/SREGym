"""SREGym entry point — invoked as ``python -m clients.aura.driver``.

Lives at ``<MEZMO_BENCH_SREGYM_ROOT>/clients/aura/driver.py`` after
``mezmo-bench sregym-run`` copies it from this repo. SREGym's
``main.py`` invokes it once per problem.

Refactored 2026-05-19 to match the real SREGym contract observed in
``clients/claudecode/driver.py``:

- Conductor URL via ``API_HOSTNAME`` / ``API_PORT`` env vars.
- Three-endpoint payload extraction (``/status``, ``/get_app``,
  ``/get_problem``).
- Instruction string built in claudecode's exact shape so the agent
  receives equivalent operational context.
- Per-stage submission via the agent itself (AURA submits through the
  bridged ``submit`` MCP tool); the driver only POSTs ``/submit`` with an
  empty solution on hard timeout, matching claudecode's fallback.

AURA is assumed to be running at ``http://127.0.0.1:<SREGYM_AURA_PORT>``
(default 8090) by the time SREGym calls this driver. The mezmo-side
orchestrator (``benchmarks/sregym/orchestrator.py``) is responsible for
that lifecycle.

Stdlib + ``httpx`` only — must remain importable inside the SREGym venv
where ``mezmo_benchmark`` is not installed.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

# SREGym repo root is two levels up (this file is installed at
# <SREGYM_ROOT>/clients/aura/driver.py). Putting it on sys.path lets us
# import Stratus's oracle classes directly so our mitigation retry loop
# uses the EXACT same pass/fail gate the leaderboard's Stratus entry uses.
_SREGYM_ROOT = Path(__file__).resolve().parents[2]
if str(_SREGYM_ROOT) not in sys.path:
    sys.path.insert(0, str(_SREGYM_ROOT))

# Local module — bundled alongside this file by the orchestrator's
# install step. Houses Constitution Principle II's enforcement boundary
# (the system-message builder refuses an ``instruction`` parameter).
try:
    from .aura_agent import (  # type: ignore
        AuraAgent,
        AuraAgentPreflightError,
        AuraRunResult,
    )
    from .system_message_builder import build_sregym_system_message  # type: ignore
except ImportError:  # pragma: no cover - defensive when invoked as a script
    from aura_agent import (  # type: ignore  # noqa: F401
        AuraAgent,
        AuraAgentPreflightError,
        AuraRunResult,
    )
    from system_message_builder import build_sregym_system_message  # type: ignore  # noqa: F401


logger = logging.getLogger("all.aura.driver")


# ----------------------------------------------------------------------
# Conductor HTTP helpers — mirror clients/claudecode/driver.py
# ----------------------------------------------------------------------


def get_api_base_url() -> str:
    """Conductor base URL. Env-driven to match the claudecode pattern."""
    host = os.getenv("API_HOSTNAME", "localhost")
    port = os.getenv("API_PORT", "8000")
    return f"http://{host}:{port}"


def get_app_info() -> dict[str, Any]:
    """GET /get_app — returns {app_name, namespace, namespaces, descriptions}."""
    url = f"{get_api_base_url()}/get_app"
    response = httpx.get(url, timeout=30.0)
    response.raise_for_status()
    return response.json()


def get_problem_id() -> str:
    """GET /get_problem — returns {problem_id}."""
    url = f"{get_api_base_url()}/get_problem"
    response = httpx.get(url, timeout=30.0)
    response.raise_for_status()
    payload = response.json()
    return str(payload.get("problem_id") or "unknown")


def wait_for_ready_stage(timeout: int = 300) -> str:
    """Poll /status until ``stage`` is ``diagnosis`` or ``mitigation``.

    Match for ``clients/claudecode/driver.py::wait_for_ready_stage``.
    Returns the stage name. Raises ``TimeoutError`` on cap exceeded.
    """
    url = f"{get_api_base_url()}/status"
    allowed_stages = {"diagnosis", "mitigation"}
    start = time.time()
    while time.time() - start < timeout:
        try:
            response = httpx.get(url, timeout=10.0)
            response.raise_for_status()
            stage = response.json().get("stage")
            if stage in allowed_stages:
                logger.info(f"Conductor ready at stage: {stage}")
                return stage
            logger.debug(f"Conductor stage={stage}; waiting for {allowed_stages}")
        except httpx.HTTPError as exc:
            logger.debug(f"Status poll failed (will retry): {exc}")
        time.sleep(1)
    raise TimeoutError(
        f"Conductor did not reach a ready stage within {timeout}s"
    )


def post_empty_solution() -> None:
    """Last-resort POST /submit with empty solution.

    Used only on the hard-timeout path so the Conductor advances and
    grades whatever state the agent left behind.
    """
    try:
        httpx.post(
            f"{get_api_base_url()}/submit",
            json={"solution": ""},
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        logger.warning(f"Empty-solution POST failed (non-fatal): {exc}")


def post_solution(answer: str) -> None:
    """POST AURA's answer to the SREGym Conductor.

    The SREGym MCP server's `/submit` mount is broken in the upstream
    cluster image (the `submit_server.py` import fails before tool
    registration, so the path 404s). SREGym's own
    `clients/claudecode` and `clients/stratus` drivers sidestep the
    MCP submit by POSTing directly to the Conductor's `/submit` HTTP
    endpoint after the agent finishes. We follow the same pattern.

    This is the per-stage submission gate: the Conductor uses this POST
    to flip from `diagnosis` → `mitigation` → `completed`, and the
    judge grades the `solution` text we send here.
    """
    try:
        response = httpx.post(
            f"{get_api_base_url()}/submit",
            json={"solution": answer},
            timeout=30.0,
        )
        logger.info(
            "Submitted to Conductor (status=%s, %d chars)",
            response.status_code, len(answer),
        )
    except httpx.HTTPError as exc:
        logger.error(f"Solution POST failed: {exc}")


# ----------------------------------------------------------------------
# Instruction builder — VERBATIM Stratus prompts (apples-to-apples vs the
# SREGym leaderboard's Stratus entry).
#
# Source: SREGym repo, pinned SHA 7a915d3b8e6d4a1c0ed1f7bb865b0d441d6fde54
#   clients/stratus/configs/diagnosis_agent_prompts.yaml
#   clients/stratus/configs/mitigation_agent_prompts.yaml
#
# These constants are the YAML `system:` and `user:` values copied byte-for-
# byte. The driver concatenates `<stratus_system>\n\n<stratus_user>` per
# stage and submits that as AURA's user message. AURA's TOML system_prompt
# is deliberately minimal (see config/aura-sregym.toml.template) so the
# model's full instruction context is Stratus's prompt and nothing else.
#
# Tool names in Stratus's prompts (submit_tool, f_submit_tool, wait_tool,
# get_traces, get_services, get_operations, get_dependency_graph,
# get_metrics, exec_read_only_kubectl_cmd, exec_kubectl_cmd_safely) refer
# to Stratus's LangGraph tool wrappers, NOT directly to MCP tools. On the
# AURA path kubectl, Prometheus, and Jaeger calls are exposed through the
# SREGym MCP bridges. Submit-style references are handled by this driver:
# the final assistant message is posted to the Conductor HTTP /submit
# endpoint after the model responds.
#
# `max_step` is filled with AURA's `turn_depth` (220) per stage — the
# value the model actually has, not Stratus's per-agent max_step of 20.
# This is the one substitution that deviates from Stratus's runtime
# numbers; see config/aura-sregym.toml.template for the rationale.
# ----------------------------------------------------------------------


# VERBATIM Stratus diagnosis_agent_prompts.yaml::system
_STRATUS_DIAGNOSIS_SYSTEM = """Monitor and diagnose an application consisting of **MANY** microservices. Some or none of the microservices have faults. Get all the pods and deployments to figure out what kind of services are running in the cluster. Carefully identify the whether the faults are present and if they are, and identify what is the root cause of the fault.

Stop diagnosis once you've found the root cause of the faults.

Go as deep as you can into what is causing the issue.

Your instructions to the tools must be clear and concise.
Your queries to tools need to be single turn.

Remember to check these, and remember this information:
## Workloads (Applications)
- **Pod**: The smallest deployable unit in Kubernetes, representing a single instance of a running application. Can contain one or more tightly coupled containers.
- **ReplicaSet**: Ensures that a specified number of pod replicas are running at all times. Often managed indirectly through Deployments.
- **Deployment**: Manages the deployment and lifecycle of applications. Provides declarative updates for Pods and ReplicaSets.
- **StatefulSet**: Manages stateful applications with unique pod identities and stable storage. Used for workloads like databases.
- **DaemonSet**: Ensures that a copy of a specific pod runs on every node in the cluster. Useful for node monitoring agents, log collectors, etc.
- **Job**: Manages batch processing tasks that are expected to complete successfully. Ensures pods run to completion.
- **CronJob**: Schedules jobs to run at specified times or intervals (similar to cron in Linux).

## Networking
- **Service**: Provides a stable network endpoint for accessing a group of pods. Types: ClusterIP, NodePort, LoadBalancer, and ExternalName.
- **Ingress**: Manages external HTTP(S) access to services in the cluster. Supports routing and load balancing for HTTP(S) traffic.
- **NetworkPolicy**: Defines rules for network communication between pods and other entities. Used for security and traffic control.

## Storage
- **PersistentVolume (PV)**: Represents a piece of storage in the cluster, provisioned by an administrator or dynamically.
- **PersistentVolumeClaim (PVC)**: Represents a request for storage by a user. Binds to a PersistentVolume.
- **StorageClass**: Defines different storage tiers or backends for dynamic provisioning of PersistentVolumes.
- **ConfigMap**: Stores configuration data as key-value pairs for applications.
- **Secret**: Stores sensitive data like passwords, tokens, or keys in an encrypted format.

## Configuration and Metadata
- **Namespace**: Logical partitioning of resources within the cluster for isolation and organization.
- **ConfigMap**: Provides non-sensitive configuration data in key-value format.
- **Secret**: Stores sensitive configuration data securely.
- **ResourceQuota**: Restricts resource usage (e.g., CPU, memory) within a namespace.
- **LimitRange**: Enforces minimum and maximum resource limits for containers in a namespace.

## Cluster Management
- **Node**: Represents a worker machine in the cluster (virtual or physical). Runs pods and is managed by the control plane.
- **ClusterRole and Role**: Define permissions for resources at the cluster or namespace level.
- **ClusterRoleBinding and RoleBinding**: Bind roles to users or groups for authorization.
- **ServiceAccount**: Associates processes in pods with permissions for accessing the Kubernetes API.

## Evaluation Pipeline
You are being evaluated as part of a two-stage pipeline: **diagnosis → mitigation**.
- **Diagnosis (current stage)**: You must identify the root cause of the fault(s). Your submission is evaluated on whether you correctly identify the faulty components and the underlying cause.
- **Mitigation (next stage)**: A separate mitigation agent will receive your diagnosis summary and attempt to fix the faults. After mitigation, the system is checked to verify that the application has sustainably recovered to a healthy state.

Your diagnosis directly impacts the success of downstream stages. Be as specific and accurate as possible.

After you finished, submit the detected faulty components to the orchestrator for evaluation.
The submission should be a natural language description of the root cause of the failure."""


# VERBATIM Stratus diagnosis_agent_prompts.yaml::user
_STRATUS_DIAGNOSIS_USER = """You will be working this application:

{app_name}

Here are some descriptions about the application:

{app_description}

It belongs to this namespace:

{app_namespace}

In each round, there is a thinking stage. In the thinking stage, you are given a list of tools. Think about what you want to call. Return your tool choice and the reasoning behind
When choosing the tool, refer to the tool by its name.
Then, there is a tool-call stage, where you make a tool_call consistent with your explanation.
You can run up to {max_step} rounds to finish the tasks.
If you call submit_tool in tool-call stage, the process will end immediately.
If you exceed this limitation, the system will force you to make a submission.
You will begin by analyzing the service's state and telemetry with the tools."""


# VERBATIM Stratus mitigation_agent_prompts.yaml::system
_STRATUS_MITIGATION_SYSTEM = """Mitigate the identified faults in an IT incident.
Some or none of the microservices have faults.
Get all the pods and deployments to figure out what kind of services are running in the cluster if you don't know what the services are.
You should carefully identify the whether the faults are present and if they are, what is the root cause of the fault.
You can stop mitigation once you've fixed all the faults.

Go as deep as you can into what is causing the issue, and mitigate the fault.

Your instructions to the tools must be clear and concise.
Your queries to tools need to be single turn.

Remember to check these, and remember this information:
## Workloads (Applications)
- **Pod**: The smallest deployable unit in Kubernetes, representing a single instance of a running application. Can contain one or more tightly coupled containers.
- **ReplicaSet**: Ensures that a specified number of pod replicas are running at all times. Often managed indirectly through Deployments.
- **Deployment**: Manages the deployment and lifecycle of applications. Provides declarative updates for Pods and ReplicaSets.
- **StatefulSet**: Manages stateful applications with unique pod identities and stable storage. Used for workloads like databases.
- **DaemonSet**: Ensures that a copy of a specific pod runs on every node in the cluster. Useful for node monitoring agents, log collectors, etc.
- **Job**: Manages batch processing tasks that are expected to complete successfully. Ensures pods run to completion.
- **CronJob**: Schedules jobs to run at specified times or intervals (similar to cron in Linux).

## Networking
- **Service**: Provides a stable network endpoint for accessing a group of pods. Types: ClusterIP, NodePort, LoadBalancer, and ExternalName.
- **Ingress**: Manages external HTTP(S) access to services in the cluster. Supports routing and load balancing for HTTP(S) traffic.
- **NetworkPolicy**: Defines rules for network communication between pods and other entities. Used for security and traffic control.

## Storage
- **PersistentVolume (PV)**: Represents a piece of storage in the cluster, provisioned by an administrator or dynamically.
- **PersistentVolumeClaim (PVC)**: Represents a request for storage by a user. Binds to a PersistentVolume.
- **StorageClass**: Defines different storage tiers or backends for dynamic provisioning of PersistentVolumes.
- **ConfigMap**: Stores configuration data as key-value pairs for applications.
- **Secret**: Stores sensitive data like passwords, tokens, or keys in an encrypted format.

## Configuration and Metadata
- **Namespace**: Logical partitioning of resources within the cluster for isolation and organization.
- **ConfigMap**: Provides non-sensitive configuration data in key-value format.
- **Secret**: Stores sensitive configuration data securely.
- **ResourceQuota**: Restricts resource usage (e.g., CPU, memory) within a namespace.
- **LimitRange**: Enforces minimum and maximum resource limits for containers in a namespace.

## Cluster Management
- **Node**: Represents a worker machine in the cluster (virtual or physical). Runs pods and is managed by the control plane.
- **ClusterRole and Role**: Define permissions for resources at the cluster or namespace level.
- **ClusterRoleBinding and RoleBinding**: Bind roles to users or groups for authorization.
- **ServiceAccount**: Associates processes in pods with permissions for accessing the Kubernetes API.

An example procedure to remediate the faults:
1) Formulate a remediation plan with a list of actionable steps.
2) Execute the plan, one step at a time.
3) Check if the plan execution worked as you desired in the IT environment.
4) If not, you can either call wait_tool to wait for it to take effect or take other actions.
5) Otherwise, continue the plan and execution process until you call f_submit_tool as you believe the application has become healthy.

## Evaluation Pipeline
You are being evaluated as part of a two-stage pipeline: **diagnosis → mitigation**.
- **Diagnosis (previous stage)**: A diagnosis agent has already identified the fault(s). Its summary is provided to you below.
- **Mitigation (current stage)**: You must fix the identified faults. Your evaluation checks whether the application is healthy after your changes. After you submit, the system will also verify that the application has **sustainably** recovered to a healthy state. This means your fixes must not only address the immediate symptoms but also ensure the system remains stable.

Make sure your remediations are thorough and the application is fully healthy before submitting.

The following is a detailed description of your tasks.

1) mitigation: Mitigate the identified faults in an IT incident with the provided tools. You can submit an empty dict "ans" with the f_submit_tool
as this task is not graded over your answer but the final result of the mitigation; therefore, you have to make sure the
application has become healthy before you call f_submit_tool."""


# VERBATIM Stratus mitigation_agent_prompts.yaml::user
_STRATUS_MITIGATION_USER = """You will be working this application:

{app_name}

Here are some descriptions about the application:

{app_description}

It belongs to this namespace:

{app_namespace}

The following is the information of faults identified by a diagnosis agent in the app:

{faults_info}

In each round, there is a thinking stage. In the thinking stage, you are given a list of tools. Think about what you want to call. Return your tool choice and the reasoning behind.
When choosing the tool, refer to the tool by its name.
Then, there is a tool-call stage, where you make a tool_call consistent with your explanation.
You can run up to {max_step} rounds to finish the tasks.
If you call f_submit_tool in tool-call stage, the process will end immediately.
If you exceed this limitation, the system will force you to make a submission.
You will begin by analyzing the service's state and telemetry with the tools."""


# Per-stage step budget that the model sees in its prompt. We populate
# Stratus's `{max_step}` placeholder with AURA's actual `turn_depth` (220)
# so the model's mental budget matches what AURA will actually allow.
# Stratus's own per-agent max_step is 20, but its 10-retry mitigation
# loop effectively gives the model 200 mitigation steps in aggregate — we
# match that 220 nominal ceiling, with the caveat (documented in
# config/aura-sregym.toml.template) that AURA spends it in one context
# per stage rather than across rollback-bounded retries.
_AURA_TURN_DEPTH = 220


def build_diagnosis_instruction(app_info: dict[str, Any]) -> str:
    """Render the user-message body AURA receives for the DIAGNOSIS stage.

    Concatenates Stratus's verbatim ``diagnosis_agent_prompts.yaml``
    ``system:`` and ``user:`` values (with ``{app_name}``, ``{app_description}``,
    ``{app_namespace}``, ``{max_step}`` substituted from the Conductor's
    ``/get_app`` response). AURA submits this as a single user message; the
    TOML ``system_prompt`` is intentionally a minimal harness preamble.

    The driver POSTs AURA's full final response to the Conductor's
    ``/submit`` endpoint (SREGym's MCP ``/submit`` mount is broken
    upstream; see ``docs/sregym.md``). AURA's
    final text IS the diagnosis answer.
    """
    app_name = app_info.get("app_name", "unknown")
    namespace = app_info.get("namespace", "default")
    namespaces = app_info.get("namespaces") or [namespace]
    # Stratus's user template takes a single `{app_namespace}` value. When
    # SREGym reports multiple namespaces (rare; only the multi-namespace
    # scenarios), join them comma-separated so the model still sees the
    # full surface — this is the one place Stratus's template has no
    # native rendering, so we approximate.
    namespace_value = (
        ", ".join(namespaces) if len(namespaces) > 1 else namespaces[0]
    )
    descriptions = app_info.get("descriptions", "")

    user_msg = _STRATUS_DIAGNOSIS_USER.format(
        app_name=app_name,
        app_description=descriptions,
        app_namespace=namespace_value,
        max_step=_AURA_TURN_DEPTH,
    )
    instruction = f"{_STRATUS_DIAGNOSIS_SYSTEM}\n\n{user_msg}"
    logger.info("Diagnosis instruction (first 200 chars): %s", instruction[:200])
    return instruction


def build_mitigation_instruction(
    app_info: dict[str, Any],
    diagnosis_answer: str,
) -> str:
    """Render the user-message body AURA receives for MITIGATION.

    Concatenates Stratus's verbatim ``mitigation_agent_prompts.yaml``
    ``system:`` and ``user:`` values, with the diagnosis answer slotted
    into Stratus's ``{faults_info}`` placeholder.
    """
    app_name = app_info.get("app_name", "unknown")
    namespace = app_info.get("namespace", "default")
    namespaces = app_info.get("namespaces") or [namespace]
    namespace_value = (
        ", ".join(namespaces) if len(namespaces) > 1 else namespaces[0]
    )
    descriptions = app_info.get("descriptions", "")

    user_msg = _STRATUS_MITIGATION_USER.format(
        app_name=app_name,
        app_description=descriptions,
        app_namespace=namespace_value,
        faults_info=diagnosis_answer,
        max_step=_AURA_TURN_DEPTH,
    )
    instruction = f"{_STRATUS_MITIGATION_SYSTEM}\n\n{user_msg}"
    logger.info("Mitigation instruction (first 200 chars): %s", instruction[:200])
    return instruction


# VERBATIM Stratus mitigation_agent_prompts.yaml::retry_user
_STRATUS_MITIGATION_RETRY_USER = """The result from the last attempt of mitigation is as follows:

{last_result}

There are some reflections from the previous run:

{reflection}

Next, use the provided tools to mitigate the faults.
It is a good habit to verify the information of faults first before you take any actions for mitigation."""


def build_mitigation_retry_instruction(
    app_info: dict[str, Any],
    diagnosis_answer: str,
    last_result: str,
    reflection: str,
) -> str:
    """Render the retry mitigation instruction.

    Mirrors Stratus's ``retry_run_initial_messages`` construction in
    ``clients/stratus/stratus_agent/driver/driver.py``: first attempt's
    full mitigation prompt + ``\\n\\n`` + the verbatim ``retry_user``
    template (with ``{last_result}`` from the oracle failure and
    ``{reflection}`` from the LLM summary of the previous attempt).
    """
    base = build_mitigation_instruction(app_info, diagnosis_answer)
    retry_suffix = _STRATUS_MITIGATION_RETRY_USER.format(
        last_result=last_result,
        reflection=reflection,
    )
    return f"{base}\n\n{retry_suffix}"


# ----------------------------------------------------------------------
# Stratus retry-mode oracle + LLM-reflection helpers
#
# Implements the moral equivalent of Stratus's `retry_mode: validate`
# pipeline (clients/stratus/stratus_agent/driver/driver.py::mitigation
# _task_main) without the deterministic-rollback step:
#
#   for attempt in 0..max:
#       run AURA mitigation with verbatim Stratus prompt (retry_user
#         template appended on attempts > 0)
#       validate ClusterStateOracle + AlertOracle
#       if all pass: submit, break
#       else: LLM-summarize attempt's transcript into a reflection
#             string for the next prompt
#
# We omit perform_rollback because SREGym's rollback stack is
# session-id-scoped on the MCP server. AURA opens a fresh MCP session
# per `agent.run()` invocation, so the prior attempt's rollback stack
# is unreachable from a follow-up Python-side call. This makes our
# implementation Stratus's `retry_mode: naive` (with reflection) rather
# than `validate` (with rollback). Documented in the run8 writeup.
# ----------------------------------------------------------------------


def _build_oracles(namespace: str) -> list[Any]:
    """Construct Stratus's two weak oracles for the active namespace.

    Imported lazily because (1) we want to fail loudly if SREGym's
    Python deps aren't on path, (2) `from kubernetes import client,
    config` inside ClusterStateOracle.validate has its own import cost.
    """
    from clients.stratus.weak_oracles.alert_oracle import AlertOracle  # type: ignore
    from clients.stratus.weak_oracles.cluster_state_oracle import ClusterStateOracle  # type: ignore

    return [ClusterStateOracle(), AlertOracle(namespace=namespace)]


def _validate_oracles(oracles: list[Any], *, namespace: str) -> tuple[bool, str]:
    """Run each oracle. Returns ``(all_succeeded, summary_str)``.

    Calls ``oracle.validate()`` with **no args** to match Stratus's
    `clients/stratus/stratus_agent/driver/driver.py::validate_oracles`
    verbatim. Namespace is baked into AlertOracle via __init__;
    ClusterStateOracle defaults its namespace param to "default" (which
    in Stratus's setup is empty, making that oracle effectively a
    "cluster reachable + control-plane responsive" liveness probe rather
    than a workload health check). The ``namespace`` parameter on this
    helper is kept for caller-side clarity and future tightening.

    ``summary_str`` formats failures as ``OracleName: [issues, ...]`` so
    it can be slotted into the Stratus ``retry_user`` ``{last_result}``
    placeholder verbatim.
    """
    from clients.stratus.weak_oracles.base_oracle import OracleResult  # type: ignore

    _ = namespace  # kept for caller-side context; oracles use their own state
    all_ok = True
    parts: list[str] = []
    for oracle in oracles:
        try:
            result: OracleResult = oracle.validate()
        except Exception as exc:  # noqa: BLE001 — surface every oracle error
            logger.exception("Oracle %s crashed: %s", oracle, exc)
            all_ok = False
            parts.append(f"{oracle.__class__.__name__}: validation error: {exc}")
            continue

        if not result.success:
            all_ok = False
            parts.append(f"{oracle.__class__.__name__}: {result.issues}")
        else:
            parts.append(f"{oracle.__class__.__name__}: PASS")

    return all_ok, " | ".join(parts)


# Verbatim from SREGym clients/stratus/configs/llm_summarization_prompt.yaml
# ::mitigation_retry_prompt. Used as the "reflection" LLM call's system
# prompt before each retry mitigation attempt.
_STRATUS_REFLECTION_SYSTEM = """Summarize a human-LLM (large language model) conversation by carefully analyzing the interaction, identifying both successes and failures before writing any summaries. For each identified point, select a direct excerpt from the message list that best illustrates or justifies your summary. Then, generate two lists: one of summaries describing what worked well in the conversation, and another of summaries describing what didn't work or where the interaction was unsuccessful.

Be sure to:
- First thoroughly review the entire message list and reason step-by-step to identify all relevant successes and failures before producing any summary points.
- For **each** bullet point in your lists, include a direct excerpt from the message(s) being referenced. Integrate the excerpt as a supporting quote within the summary bullet, clearly separating the summary from the quoted message.
- Consider the intentions of the human participant, how well the LLM addressed those goals, communication clarity, misunderstandings, helpfulness, responsiveness, and overall satisfaction.
- Continue analyzing the conversation until all notable successes and failures are identified.

Structure your output as follows:
- First a "What Worked" section, then a "What Didn't Work" section.
- Each section should use a markdown header.
- Use bullet points for each item (2-5 per section). Each bullet point should be succinct (no more than two sentences).
- Explicitly include a representative excerpt from the conversation in every bullet (formatted as a quote, e.g., "User: ..." or "LLM: ...").

# Output Format

- Use markdown headers: "What Worked" and "What Didn't Work".
- Present 2-5 bullet points per list, each containing both a concise summary and a quoted message excerpt from the conversation.
- Each bullet may have the structure: Brief summary sentence, followed by a colon and a quoted excerpt.
- Do not include any extraneous explanation or text."""


def _extract_transcript_snippets(transcript_path: Path, max_chars: int = 80_000) -> str:
    """Read AURA's per-attempt transcript and emit a readable digest.

    The transcript is line-delimited JSON of OpenAI-style chat events
    (one per line, written by aura_agent._record_event). For the
    summarizer we only care about `choices[].delta.content` (assistant
    text) and `choices[].delta.tool_calls[]` (tool invocations). We
    flatten those into ``role: text`` / ``tool: name(args)`` lines.

    Truncates at ``max_chars`` from the END (we want the last interactions,
    not the opening preamble) so the reflection LLM call stays well inside
    the model's context budget even on long transcripts.
    """
    if not transcript_path.exists():
        return "(transcript missing)"

    lines: list[str] = []
    try:
        for raw in transcript_path.read_text(encoding="utf-8", errors="strict").splitlines():
            raw = raw.strip()
            if not raw or not raw.startswith("{"):
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            choices = event.get("choices") or []
            for choice in choices:
                delta = choice.get("delta") or {}
                content = delta.get("content")
                if isinstance(content, str) and content.strip():
                    lines.append(f"LLM: {content.strip()}")
                tool_calls = delta.get("tool_calls") or []
                for tc in tool_calls:
                    fn = (tc.get("function") or {})
                    name = fn.get("name") or "?"
                    args = fn.get("arguments") or ""
                    # Truncate per-tool arguments for digest readability.
                    args_short = args[:200] + ("…" if len(args) > 200 else "")
                    lines.append(f"TOOL_CALL: {name}({args_short})")
    except OSError as exc:
        return f"(transcript read failed: {exc})"

    joined = "\n".join(lines)
    if len(joined) <= max_chars:
        return joined or "(empty transcript)"
    return "(…earlier turns elided…)\n" + joined[-max_chars:]


def _generate_reflection(
    *,
    transcript_path: Path,
    model_id: str,
    aura_port: int,
) -> str:
    """LLM-summarize the previous attempt's transcript.

    Re-uses AURA's chat completions endpoint with the SAME model the
    mitigation attempts use (model_id from --model). This matches the
    intent of Stratus's `generate_run_summary` which calls
    `get_llm_backend_for_agent()` (same backend as the mitigation
    agent) with the verbatim retry summary prompt.

    Why through AURA instead of LiteLLM directly: keeps Bedrock auth
    routing identical to the mitigation path. AURA is already running.
    """
    digest = _extract_transcript_snippets(transcript_path)
    payload = {
        "model": "aura-sregym",
        "messages": [
            {"role": "system", "content": _STRATUS_REFLECTION_SYSTEM},
            {
                "role": "user",
                "content": (
                    "Here is the conversation from the previous mitigation "
                    "attempt to summarize:\n\n"
                    f"{digest}"
                ),
            },
        ],
        # Reflection is a one-shot summarization — no tool loop needed.
        "stream": False,
    }
    url = f"http://127.0.0.1:{aura_port}/v1/chat/completions"
    try:
        response = httpx.post(url, json=payload, timeout=120.0)
        response.raise_for_status()
        body = response.json()
        choice = (body.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content")
        if isinstance(text, str) and text.strip():
            return text.strip()
        logger.warning("Reflection LLM returned no text; body=%s", body)
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        logger.warning("Reflection generation failed: %s", exc)
    # Fall back to a minimal reflection so the retry prompt still has
    # SOMETHING — better than crashing the whole run.
    return "(reflection unavailable; previous attempt did not fix the cluster)"


# ----------------------------------------------------------------------
# Result persistence
# ----------------------------------------------------------------------


def _now_iso_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def save_run_metadata(
    *,
    run_dir: Path,
    problem_id: str,
    stage: str,
    result: AuraRunResult,
) -> Path:
    """Per-problem result.json the orchestrator's CSV-augmentation reads."""
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "problem_id": problem_id,
        "stage": stage,
        "process_status": result.process_status,
        "duration_seconds": result.duration_seconds,
        "transcript_path": str(result.transcript_path),
        "answer_length_chars": len(result.answer),
        "timestamp_utc": _now_iso_utc(),
    }
    target = run_dir / "result.json"
    target.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        errors="strict",
    )
    return target


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------


def _wait_for_stage(target_stages: set[str], timeout: int) -> str | None:
    """Block until Conductor `stage` matches one of ``target_stages`` or returns None on timeout."""
    url = f"{get_api_base_url()}/status"
    start = time.time()
    while time.time() - start < timeout:
        try:
            response = httpx.get(url, timeout=10.0)
            response.raise_for_status()
            stage = response.json().get("stage")
            if stage in target_stages:
                return stage
        except httpx.HTTPError:
            pass
        time.sleep(1)
    return None


def _run_stage(
    *,
    agent: AuraAgent,
    instruction: str,
    problem_id: str,
    namespace: str,
    submission_endpoint: str,
    run_dir: Path,
    stage_label: str,
) -> AuraRunResult:
    """Invoke AURA + persist per-stage artifacts. Returns the AuraRunResult."""
    result = agent.run(
        instruction,
        problem_id=problem_id,
        namespace=namespace,
        submission_endpoint=submission_endpoint,
    )
    save_run_metadata(
        run_dir=run_dir,
        problem_id=problem_id,
        stage=stage_label,
        result=result,
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run AURA against a SREGym problem"
    )
    parser.add_argument(
        "--model",
        default=os.getenv(
            "AGENT_MODEL_ID",
            "bedrock/us.anthropic.claude-sonnet-4-6",
        ),
        help="Upstream model id (LiteLLM convention with provider prefix).",
    )
    parser.add_argument(
        "--logs-dir",
        default=os.getenv("AGENT_LOGS_DIR", "./logs/aura"),
    )
    parser.add_argument(
        "--aura-port",
        type=int,
        default=int(os.getenv("SREGYM_AURA_PORT", "8090")),
    )
    args = parser.parse_args(argv)

    logger.info("=" * 80)
    logger.info("Starting AURA agent for SREGym")
    logger.info(f"Model: {args.model}")
    logger.info(f"Logs directory: {args.logs_dir}")
    logger.info(f"AURA port: {args.aura_port}")
    logger.info("=" * 80)

    # Wait for Conductor to reach diagnosis stage.
    try:
        stage = wait_for_ready_stage(timeout=300)
    except TimeoutError as exc:
        logger.error(f"Conductor never reached a ready stage: {exc}")
        return 1
    if stage != "diagnosis":
        logger.error(f"Expected stage=diagnosis on entry, got stage={stage}")
        return 1

    try:
        app_info = get_app_info()
        problem_id = get_problem_id()
    except httpx.HTTPError as exc:
        logger.error(f"Failed to fetch problem context from Conductor: {exc}")
        return 1
    logger.info(f"Problem: {problem_id}; app: {app_info.get('app_name')}")

    agent_timeout = int(os.getenv("AGENT_TIMEOUT", "600"))
    aura_config_env = os.getenv("MEZMO_BENCH_AURA_SREGYM_CONFIG")
    aura_config_path = Path(aura_config_env) if aura_config_env else None
    namespace = app_info.get("namespace") or (
        app_info.get("namespaces") or ["default"]
    )[0]
    submission_endpoint = f"{get_api_base_url()}/submit"

    # ---- Stage 1: DIAGNOSIS ----
    diagnosis_dir = Path(args.logs_dir) / problem_id / "stage_diagnosis"
    diagnosis_dir.mkdir(parents=True, exist_ok=True)
    diagnosis_agent = AuraAgent(
        model_id="aura-sregym",
        aura_port=args.aura_port,
        transcript_dir=diagnosis_dir,
        request_timeout_seconds=float(agent_timeout),
        aura_config_path=aura_config_path,
    )
    try:
        diagnosis_agent.preflight()
    except AuraAgentPreflightError as exc:
        logger.error(f"AURA preflight failed: {exc}")
        return 2

    try:
        diagnosis_result = _run_stage(
            agent=diagnosis_agent,
            instruction=build_diagnosis_instruction(app_info),
            problem_id=problem_id,
            namespace=str(namespace),
            submission_endpoint=submission_endpoint,
            run_dir=diagnosis_dir,
            stage_label="diagnosis",
        )
    except Exception as exc:  # noqa: BLE001 — top-level guard
        logger.exception("AURA invocation crashed in diagnosis: %s", exc)
        post_empty_solution()
        return 1

    # POST AURA's diagnosis answer to the Conductor (claudecode pattern).
    if diagnosis_result.process_status == "ok" and diagnosis_result.answer.strip():
        post_solution(diagnosis_result.answer)
    else:
        logger.warning(
            "AURA diagnosis returned no usable answer (status=%s, len=%d); "
            "posting empty solution",
            diagnosis_result.process_status,
            len(diagnosis_result.answer),
        )
        post_empty_solution()

    # ---- Stage 2: MITIGATION (if Conductor advances) ----
    mitigation_stage = _wait_for_stage(
        target_stages={"mitigation", "completed", "done"},
        timeout=60,
    )
    if mitigation_stage != "mitigation":
        logger.info(
            "Conductor advanced past mitigation (stage=%s); driver done.",
            mitigation_stage,
        )
        return 0

    # ---- Mitigation retry loop — Stratus's `retry_mode: naive` semantics ----
    # Up to 10 mitigation attempts; oracle-gated; LLM reflection between
    # attempts feeds Stratus's verbatim `retry_user` template. We omit
    # deterministic kubectl rollback (Stratus's `validate` mode) — see
    # `_build_oracles` docstring above for the MCP session-id reason.
    max_attempts = int(os.getenv("AURA_MIT_MAX_ATTEMPTS", "10"))
    oracles = _build_oracles(namespace=str(namespace))

    last_mitigation_result: AuraRunResult | None = None
    last_oracle_summary: str = ""

    for attempt in range(1, max_attempts + 1):
        attempt_dir = (
            Path(args.logs_dir) / problem_id / f"stage_mitigation_attempt_{attempt}"
        )
        attempt_dir.mkdir(parents=True, exist_ok=True)
        mitigation_agent = AuraAgent(
            model_id="aura-sregym",
            aura_port=args.aura_port,
            transcript_dir=attempt_dir,
            request_timeout_seconds=float(agent_timeout),
            aura_config_path=aura_config_path,
        )

        if attempt == 1:
            instruction = build_mitigation_instruction(
                app_info, diagnosis_result.answer
            )
        else:
            # Build reflection from the PREVIOUS attempt's transcript so
            # the model sees what went wrong before retrying.
            assert last_mitigation_result is not None  # mypy guard
            reflection = _generate_reflection(
                transcript_path=last_mitigation_result.transcript_path,
                model_id=args.model,
                aura_port=args.aura_port,
            )
            instruction = build_mitigation_retry_instruction(
                app_info,
                diagnosis_result.answer,
                last_result=last_oracle_summary,
                reflection=reflection,
            )
            logger.info(
                "Mitigation attempt %d: retry context injected (last_result %d chars, "
                "reflection %d chars)",
                attempt,
                len(last_oracle_summary),
                len(reflection),
            )

        try:
            mitigation_result = _run_stage(
                agent=mitigation_agent,
                instruction=instruction,
                problem_id=problem_id,
                namespace=str(namespace),
                submission_endpoint=submission_endpoint,
                run_dir=attempt_dir,
                stage_label=f"mitigation_attempt_{attempt}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "AURA invocation crashed in mitigation attempt %d: %s",
                attempt,
                exc,
            )
            # Don't abandon the problem — record the crash and move on to
            # the next attempt (or exit-the-loop submit if this was the
            # last). A single AURA hiccup shouldn't burn the whole run.
            last_mitigation_result = None
            last_oracle_summary = (
                f"Previous attempt CRASHED during execution: {exc}"
            )
            if attempt == max_attempts:
                break
            continue

        last_mitigation_result = mitigation_result

        # Validate cluster + alert oracles; pass = done.
        all_ok, oracle_summary = _validate_oracles(
            oracles, namespace=str(namespace)
        )
        last_oracle_summary = oracle_summary
        logger.info(
            "Mitigation attempt %d/%d oracle result: %s | summary=%s",
            attempt,
            max_attempts,
            "PASS" if all_ok else "FAIL",
            oracle_summary,
        )
        if all_ok:
            logger.info(
                "Oracles passed on attempt %d; submitting and exiting loop.",
                attempt,
            )
            break

    # Mitigation submission is always the empty string per the SREGym
    # contract — the Conductor grades whether the cluster is healthy
    # post-fix, not the agent's words.
    post_empty_solution()

    last_status = (
        last_mitigation_result.process_status if last_mitigation_result else "crashed"
    )
    last_duration = (
        last_mitigation_result.duration_seconds if last_mitigation_result else 0.0
    )
    logger.info("=" * 80)
    logger.info(
        "AURA SREGym run completed (diagnosis=%s/%.1fs, "
        "mitigation_attempts=%d, last_mit=%s/%.1fs)",
        diagnosis_result.process_status,
        diagnosis_result.duration_seconds,
        attempt,
        last_status,
        last_duration,
    )
    logger.info("=" * 80)
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("AURA_DRIVER_LOG_LEVEL", "INFO"),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    sys.exit(main())

"""Chart-mounted Saleor loadgen entrypoint for persisted GUC visibility.

The grading DSN intentionally carries a statement-timeout safety fence. That
fence becomes the session's active pg_settings source and can hide a lower
priority ALTER SYSTEM value. Merge the effective persisted file settings under
the existing runtime snapshot, while retaining role/database settings and
catalog state collected by the image-shipped hook.

This file is mounted by the task chart. It does not modify or rebuild the
pinned loadgen base image.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import ssl
import sys
import urllib.request
from pathlib import Path
from typing import Any

import evidence_collector as assemble
import grader_hooks
import loadgen_grader_common as grader_common
import psycopg
import yaml

_image_collect_runtime_snapshot = grader_hooks.collect_runtime_snapshot
_image_build_docker_state = assemble.build_docker_state

_SERVICE_ACCOUNT_ROOT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_POD_SELECTORS = {
    "svc-saleor-api": ("app.kubernetes.io/component", "saleor-api"),
    "svc-saleor-worker": ("app.kubernetes.io/component", "saleor-worker"),
    "db": ("app.kubernetes.io/name", "postgres"),
}
_RESTART_ONLY_COMPONENTS: list[str] = []
_PROTECTED_CONTENT_TABLES: list[str] = []
_CONTENT_BASELINE_IDS: dict[str, list[str]] = {}
_CONTENT_DIGEST_PHASES: dict[str, dict[str, dict[str, Any]]] = {}
_CONTENT_DIGEST_ALGORITHM = "sha256-length-prefixed-id-jsonb-v1"
_TASK_COLLECTOR_PATH = Path("/grader-runtime/task-protected-collector.py")
_PROTECTED_POSTGRES_PATH = Path("/grader-runtime/protected_postgres.py")
_MAX_TASK_COLLECTOR_RESULT_BYTES = 1_048_576
_ASYNC_BUNDLE_FILES = ("webhooks.jsonl", "async_integrity.json")
_PROMETHEUS_METRIC_NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")


def _install_async_bundle_files() -> None:
    """Seal Saleor's explicit async ledgers into the authenticated bundle."""
    existing = tuple(grader_common.BUNDLE_FILES)
    missing = tuple(name for name in _ASYNC_BUNDLE_FILES if name not in existing)
    grader_common.BUNDLE_FILES = (*existing, *missing)


def _install_async_metric_allowlist(sidecar: Any, manifest: dict[str, Any]) -> None:
    """Bound configured Prometheus capture to the task's required families."""
    config = manifest.get("async_metrics")
    if config is None:
        return
    if not isinstance(config, dict) or set(config) != {"allowed_names"}:
        raise RuntimeError("async_metrics must contain only allowed_names")
    configured = config["allowed_names"]
    if not isinstance(configured, list) or not configured:
        raise RuntimeError("async_metrics.allowed_names must be a nonempty list")
    if any(
        not isinstance(name, str)
        or _PROMETHEUS_METRIC_NAME.fullmatch(name) is None
        for name in configured
    ):
        raise RuntimeError("async_metrics.allowed_names contains an invalid metric name")
    names = tuple(configured)
    if len(set(names)) != len(names):
        raise RuntimeError("async_metrics.allowed_names contains an invalid metric name")
    original = sidecar.parse_exposition

    def parse_selected(text: str) -> list[dict[str, Any]]:
        rows = original(text)
        return [row for row in rows if row.get("name") in names]

    sidecar.parse_exposition = parse_selected


def _collect_runtime_snapshot() -> dict[str, Any]:
    snapshot = _image_collect_runtime_snapshot()
    postgres = snapshot.get("postgres")
    if not isinstance(postgres, dict):
        raise RuntimeError("Saleor runtime snapshot has no postgres mapping; refusing to grade")

    dsn = os.environ.get(grader_hooks.PG_SNAPSHOT_DSN_ENV, "")
    if not dsn:
        raise RuntimeError(
            f"{grader_hooks.PG_SNAPSHOT_DSN_ENV} is required for persisted GUC capture"
        )
    with psycopg.connect(dsn, connect_timeout=10) as connection:
        rows = connection.execute(
            "SELECT DISTINCT ON (lower(name)) lower(name), setting "
            "FROM pg_file_settings "
            "WHERE applied AND error IS NULL AND name IS NOT NULL "
            "ORDER BY lower(name), seqno DESC"
        ).fetchall()
        scoped_rows = connection.execute(
            "SELECT lower(split_part(setting, '=', 1)) "
            "FROM pg_db_role_setting CROSS JOIN LATERAL unnest(setconfig) AS setting "
            "WHERE position('=' in setting) > 1"
        ).fetchall()

    persisted = {
        str(name): grader_hooks._coerce(str(value))  # noqa: SLF001
        for name, value in rows
    }
    scoped_names = {str(row[0]) for row in scoped_rows}
    # The image hook's long-lived grading session can retain the pre-reload
    # value for a configuration-file GUC.  pg_file_settings is authoritative
    # for those persisted values.  Keep role/database settings and catalog
    # state from the image hook, however: scoped settings intentionally outrank
    # the system file and must remain visible to minimality.
    merged = dict(postgres)
    merged.update({name: value for name, value in persisted.items() if name not in scoped_names})
    snapshot["postgres"] = merged
    return snapshot


grader_hooks.collect_runtime_snapshot = _collect_runtime_snapshot


_image_safe_load = yaml.safe_load


def _safe_load_with_task_postgres_capture(stream: Any) -> Any:
    """Enable dormant PostgreSQL capture for an answer key that opts into it.

    The pinned sidecar already implements the before/after collector, but its
    activation predates task-owned verifiers and is keyed to a shared
    materializer name. Keep the public verifier contract on trusted main code:
    only a Saleor manifest that explicitly requests protected PostgreSQL
    capture and declares a v2 task-owned verifier receives the dormant selector.
    The original answer-key bytes served to the task verifier remain unchanged.
    """

    global _PROTECTED_CONTENT_TABLES, _RESTART_ONLY_COMPONENTS

    document = _image_safe_load(stream)
    if not isinstance(document, dict):
        return document
    restart_invariants = document.get("restart_invariants")
    if restart_invariants is not None:
        if not isinstance(restart_invariants, dict) or set(restart_invariants) != {"components"}:
            raise RuntimeError("Saleor restart_invariants must contain exactly a components list")
        components = restart_invariants.get("components")
        if (
            not isinstance(components, list)
            or not components
            or any(not isinstance(name, str) or not name for name in components)
            or len(components) != len(set(components))
        ):
            raise RuntimeError(
                "Saleor restart_invariants.components must be a non-empty unique string list"
            )
        unknown = sorted(set(components) - set(_POD_SELECTORS))
        if unknown:
            raise RuntimeError(f"Saleor restart_invariants has no fixed pod selector for {unknown}")
        _RESTART_ONLY_COMPONENTS = list(components)
    postgres = document.get("postgres_invariants")
    verification = document.get("verification")
    if not (
        isinstance(postgres, dict)
        and postgres.get("capture_for_task_verifier") is True
        and isinstance(verification, dict)
        and verification.get("version") == 2
        and isinstance(verification.get("task_verifier"), dict)
    ):
        if isinstance(postgres, dict) and "protected_content_tables" in postgres:
            raise RuntimeError(
                "Saleor protected_content_tables requires task-owned PostgreSQL capture"
            )
        return document
    content_tables = postgres.get("protected_content_tables")
    if content_tables is not None:
        protected_tables = postgres.get("protected_tables")
        if (
            not isinstance(content_tables, list)
            or not content_tables
            or len(content_tables) != len(set(content_tables))
            or any(not isinstance(table, str) or not table for table in content_tables)
        ):
            raise RuntimeError(
                "Saleor protected_content_tables must be a non-empty unique string list"
            )
        if not isinstance(protected_tables, list) or not set(content_tables) <= set(
            protected_tables
        ):
            raise RuntimeError(
                "Saleor protected_content_tables must be a subset of protected_tables"
            )
        invalid = sorted(
            table for table in content_tables if re.fullmatch(r"[a-z_][a-z0-9_]*", table) is None
        )
        if invalid:
            raise RuntimeError(
                f"Saleor protected_content_tables contains invalid identifiers {invalid}"
            )
        _PROTECTED_CONTENT_TABLES = list(content_tables)
    materializers = verification.get("materializers")
    if not isinstance(materializers, list) or any(
        not isinstance(name, str) for name in materializers
    ):
        raise RuntimeError("Saleor task-owned PostgreSQL capture has malformed materializers")
    if "postgres_invariants" not in materializers:
        materializers.append("postgres_invariants")
    return document


yaml.safe_load = _safe_load_with_task_postgres_capture


def _load_python_module(name: str, path: Path) -> Any:
    if not path.is_file():
        raise RuntimeError(f"Saleor protected collector module is missing: {path}")
    module_spec = importlib.util.spec_from_file_location(name, path)
    if module_spec is None or module_spec.loader is None:
        raise RuntimeError(f"cannot load Saleor protected collector module: {path}")
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[name] = module
    module_spec.loader.exec_module(module)
    return module


def _task_collector_contract(manifest: dict[str, Any]) -> tuple[Any, Any, str] | None:
    raw = manifest.get("protected_collector")
    if raw is None:
        if _TASK_COLLECTOR_PATH.exists():
            raise RuntimeError(
                "task-protected-collector.py is staged without a protected_collector contract"
            )
        return None
    if not isinstance(raw, dict) or set(raw) != {
        "version",
        "entrypoint",
        "sha256",
        "config",
    }:
        raise RuntimeError(
            "Saleor protected_collector must contain version, entrypoint, sha256, and config"
        )
    if raw["version"] != 1:
        raise RuntimeError("Saleor protected_collector.version must be 1")
    postgres = manifest.get("postgres_invariants")
    verification = manifest.get("verification")
    if not (
        isinstance(postgres, dict)
        and postgres.get("capture_for_task_verifier") is True
        and isinstance(verification, dict)
        and verification.get("version") == 2
        and isinstance(verification.get("task_verifier"), dict)
    ):
        raise RuntimeError(
            "Saleor protected_collector requires protected PostgreSQL capture "
            "and a verifier task verifier"
        )
    if not isinstance(raw["entrypoint"], str) or not raw["entrypoint"].endswith(".py"):
        raise RuntimeError("Saleor protected_collector.entrypoint must name a Python file")
    expected = raw["sha256"]
    if (
        not isinstance(expected, str)
        or not expected.startswith("sha256:")
        or len(expected) != 71
    ):
        raise RuntimeError("Saleor protected_collector.sha256 is malformed")
    if not _TASK_COLLECTOR_PATH.is_file():
        raise RuntimeError("Saleor protected_collector entrypoint was not staged into the chart")
    actual = "sha256:" + hashlib.sha256(_TASK_COLLECTOR_PATH.read_bytes()).hexdigest()
    if actual != expected:
        raise RuntimeError(
            "Saleor protected_collector digest mismatch: "
            f"expected={expected}, actual={actual}"
        )
    _load_python_module("protected_postgres", _PROTECTED_POSTGRES_PATH)
    module = _load_python_module("task_protected_collector", _TASK_COLLECTOR_PATH)
    capture = getattr(module, "capture", None)
    if not callable(capture):
        raise RuntimeError("Saleor task protected collector must export capture()")
    return capture, raw["config"], actual


def _bounded_collector_result(value: object, *, phase: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(
            f"Saleor protected collector {phase} capture did not return a mapping"
        )
    try:
        encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Saleor protected collector {phase} capture is not JSON-serializable: {exc}"
        ) from exc
    if len(encoded) > _MAX_TASK_COLLECTOR_RESULT_BYTES:
        raise RuntimeError(
            f"Saleor protected collector {phase} capture exceeds "
            f"{_MAX_TASK_COLLECTOR_RESULT_BYTES} bytes"
        )
    return value


def _install_task_protected_collector(sidecar: Any, manifest: dict[str, Any]) -> None:
    contract = _task_collector_contract(manifest)
    if contract is None:
        return
    capture, config, collector_digest = contract
    original = sidecar._capture_postgres_invariant_phase
    before_capture: dict[str, Any] | None = None

    def capture_phase(
        sidecar_manifest: dict[str, Any],
        runtime_capture: dict[str, Any],
        *,
        identity_basis: dict[str, list[str]] | None = None,
    ) -> dict[str, Any]:
        nonlocal before_capture
        phase = "before" if identity_basis is None else "after"
        result = original(
            sidecar_manifest,
            runtime_capture,
            identity_basis=identity_basis,
        )
        dsn = os.environ.get("PG_ADMIN_DSN", "")
        if not dsn:
            raise RuntimeError("Saleor protected collector requires PG_ADMIN_DSN")
        with psycopg.connect(dsn, connect_timeout=10) as connection:
            current = _bounded_collector_result(
                capture(connection=connection, config=config, phase=phase),
                phase=phase,
            )
        if phase == "before":
            before_capture = current
            return result
        if before_capture is None:
            raise RuntimeError("Saleor protected collector final capture has no pre-agent baseline")
        output = {
            "schema_version": 1,
            "scenario": manifest.get("scenario"),
            "collector_sha256": collector_digest,
            "before": before_capture,
            "after": current,
        }
        target = Path(os.environ.get("GRADER_DIR", "/grader")) / "sut" / "task-protected-collector.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(output, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(target)
        return result

    sidecar._capture_postgres_invariant_phase = capture_phase


def _read_pod_restarts(required_components: list[str]) -> dict[str, int]:
    """Read real restart counts through the loadgen's namespaced pod-reader RBAC.

    The image-shipped sidecar currently passes an empty restart mapping into the
    shared evidence assembler.  Defaulting that empty mapping to zero makes the
    anti-restart gate vacuous.  This chart-mounted shim closes the gap without
    modifying the pinned loadgen image: every required component must resolve to
    at least one Kubernetes pod with container status evidence, and every
    container restart is summed.

    Readiness is deliberately not enforced here.  ``docker_state.json`` carries
    two independent facts: service probes determine the outcome's ``running``
    fields, while this Kubernetes read supplies restart counts for safety.  A
    stopped or unready service is a legitimate failed outcome whose restart
    evidence must still be finalized and available to the verifier.
    """

    unknown = sorted(set(required_components) - set(_POD_SELECTORS))
    if unknown:
        raise RuntimeError(f"Saleor restart evidence has no fixed pod selector for {unknown}")
    token_path = _SERVICE_ACCOUNT_ROOT / "token"
    namespace_path = _SERVICE_ACCOUNT_ROOT / "namespace"
    ca_path = _SERVICE_ACCOUNT_ROOT / "ca.crt"
    for path in (token_path, namespace_path, ca_path):
        if not path.is_file():
            raise RuntimeError(
                f"Saleor restart evidence requires the Kubernetes service-account file {path}"
            )
    token = token_path.read_text().strip()
    namespace = namespace_path.read_text().strip()
    if not token or not namespace:
        raise RuntimeError("Saleor restart evidence service-account token/namespace is empty")

    request = urllib.request.Request(
        f"https://kubernetes.default.svc/api/v1/namespaces/{namespace}/pods",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    context = ssl.create_default_context(cafile=str(ca_path))
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=context),
    )
    try:
        with opener.open(request, timeout=15) as response:
            document = json.loads(response.read())
    except Exception as exc:  # noqa: BLE001 - missing protected evidence is terminal
        raise RuntimeError(f"Saleor restart evidence pod-list request failed: {exc}") from exc
    items = document.get("items") if isinstance(document, dict) else None
    if not isinstance(items, list):
        raise RuntimeError("Saleor restart evidence pod-list response is malformed")

    observed = {component: 0 for component in required_components}
    matches = {component: 0 for component in required_components}
    for pod in items:
        if not isinstance(pod, dict):
            raise RuntimeError("Saleor restart evidence contains a malformed pod entry")
        labels = (pod.get("metadata") or {}).get("labels") or {}
        pod_status = pod.get("status") or {}
        statuses = pod_status.get("containerStatuses")
        if not isinstance(labels, dict):
            raise RuntimeError("Saleor restart evidence pod labels are malformed")
        for component in required_components:
            key, value = _POD_SELECTORS[component]
            if labels.get(key) != value:
                continue
            matches[component] += 1
            if not isinstance(statuses, list) or not statuses:
                raise RuntimeError(
                    f"Saleor restart evidence pod for {component} lacks containerStatuses"
                )
            for status in statuses:
                count = status.get("restartCount") if isinstance(status, dict) else None
                if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                    raise RuntimeError(
                        f"Saleor restart evidence has invalid restartCount for {component}"
                    )
                observed[component] += count
    missing = sorted(component for component, count in matches.items() if count < 1)
    if missing:
        raise RuntimeError(
            f"Saleor restart evidence did not resolve required pod components {missing}"
        )
    return observed


def _build_docker_state(
    app_running: dict[str, bool],
    db_running: bool,
    restarts: dict[str, int],
) -> dict[str, Any]:
    if restarts:
        raise RuntimeError(
            "Saleor image sidecar unexpectedly supplied restart counts; refusing ambiguous evidence"
        )
    overlap = sorted(set(app_running) & set(_RESTART_ONLY_COMPONENTS))
    if overlap:
        raise RuntimeError(f"Saleor restart-only components overlap HTTP-probed services {overlap}")
    combined_running = {
        **app_running,
        **{component: True for component in _RESTART_ONLY_COMPONENTS},
    }
    required = [*combined_running, assemble.DB_STATE_KEY]
    observed = _read_pod_restarts(required)
    return _image_build_docker_state(combined_running, db_running, observed)


assemble.build_docker_state = _build_docker_state


def _digest_content_rows(
    table: str, rows: list[tuple[Any, Any]]
) -> tuple[list[str], dict[str, Any]]:
    if not rows:
        raise RuntimeError(f"Saleor protected content table has no boot rows: public.{table}")
    identities = [str(row[0]) for row in rows]
    if len(identities) != len(set(identities)) or any(not identity for identity in identities):
        raise RuntimeError(
            f"Saleor protected content table has invalid or duplicate identities: {table}"
        )
    digest = hashlib.sha256()
    for identity, raw_content in rows:
        if not isinstance(raw_content, str):
            raise RuntimeError(
                f"Saleor protected content row is not canonical JSON text: {table}/{identity}"
            )
        identity_bytes = str(identity).encode("utf-8")
        content_bytes = raw_content.encode("utf-8")
        digest.update(len(identity_bytes).to_bytes(8, "big"))
        digest.update(identity_bytes)
        digest.update(len(content_bytes).to_bytes(8, "big"))
        digest.update(content_bytes)
    return identities, {
        "algorithm": _CONTENT_DIGEST_ALGORITHM,
        "boot_row_count": len(rows),
        "sha256": digest.hexdigest(),
    }


def _capture_protected_content_phase(phase: str) -> dict[str, dict[str, Any]]:
    if phase not in {"before", "after"}:
        raise RuntimeError(f"Saleor protected content phase is invalid: {phase!r}")
    dsn = os.environ.get(grader_hooks.PG_SNAPSHOT_DSN_ENV, "")
    if not dsn:
        raise RuntimeError(
            f"{grader_hooks.PG_SNAPSHOT_DSN_ENV} is required for protected content capture"
        )
    captured: dict[str, dict[str, Any]] = {}
    with psycopg.connect(dsn, connect_timeout=10) as connection:
        for table in _PROTECTED_CONTENT_TABLES:
            if phase == "before":
                rows = connection.execute(
                    f"SELECT id::text, to_jsonb(protected_row)::text "
                    f'FROM "{table}" AS protected_row ORDER BY id'
                ).fetchall()
            else:
                basis = _CONTENT_BASELINE_IDS.get(table)
                if not basis:
                    raise RuntimeError(
                        f"Saleor protected content final capture has no boot basis for {table}"
                    )
                rows = connection.execute(
                    f"SELECT id::text, to_jsonb(protected_row)::text "
                    f'FROM "{table}" AS protected_row '
                    "WHERE id::text = ANY(%s) ORDER BY id",
                    (basis,),
                ).fetchall()
            identities, fingerprint = _digest_content_rows(table, rows)
            if phase == "before":
                _CONTENT_BASELINE_IDS[table] = identities
            elif identities != _CONTENT_BASELINE_IDS[table]:
                raise RuntimeError(
                    f"Saleor protected content identities changed for {table}: "
                    f"expected={len(_CONTENT_BASELINE_IDS[table])} actual={len(identities)}"
                )
            captured[table] = fingerprint
    _CONTENT_DIGEST_PHASES[phase] = captured
    return captured


def _install_protected_content_collector(sidecar: Any) -> None:
    if not _PROTECTED_CONTENT_TABLES:
        return
    original_capture = getattr(sidecar, "_capture_postgres_invariant_phase", None)
    original_collect = getattr(sidecar, "_collect_episode_evidence", None)
    grader_root = getattr(sidecar, "GRADER", None)
    if not callable(original_capture) or not callable(original_collect):
        raise RuntimeError("Saleor image sidecar lacks required protected-capture hooks")
    if not isinstance(grader_root, Path):
        raise RuntimeError("Saleor image sidecar lacks a valid GRADER path")

    def capture_with_content(
        manifest: dict[str, Any],
        runtime_capture: dict[str, Any],
        *,
        identity_basis: dict[str, list[str]] | None = None,
    ) -> dict[str, Any]:
        captured = original_capture(manifest, runtime_capture, identity_basis=identity_basis)
        phase = "before" if identity_basis is None else "after"
        captured["table_content_digests"] = _capture_protected_content_phase(phase)
        return captured

    async def collect_with_content(declared: bool, postgres_before: dict[str, Any] | None) -> None:
        await original_collect(declared, postgres_before)
        if set(_CONTENT_DIGEST_PHASES) != {"before", "after"}:
            raise RuntimeError(
                "Saleor protected content capture did not produce both phases: "
                f"{sorted(_CONTENT_DIGEST_PHASES)}"
            )
        target = grader_root / "sut" / "db_state.json"
        try:
            document = json.loads(target.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - protected evidence must fail closed
            raise RuntimeError(
                f"Saleor protected content cannot read finalized db_state: {exc}"
            ) from exc
        if not isinstance(document, dict) or "table_content_digests" in document:
            raise RuntimeError(
                "Saleor protected content found malformed or ambiguous db_state evidence"
            )
        document["table_content_digests"] = {
            "before": _CONTENT_DIGEST_PHASES["before"],
            "after": _CONTENT_DIGEST_PHASES["after"],
        }
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(target)

    sidecar._capture_postgres_invariant_phase = capture_with_content
    sidecar._collect_episode_evidence = collect_with_content


def main() -> None:
    manifest_path = Path("/grader-key/ground-truth.yaml")
    if not manifest_path.is_file():
        raise RuntimeError("Saleor loadgen entrypoint requires /grader-key/ground-truth.yaml")
    manifest = _safe_load_with_task_postgres_capture(manifest_path.read_text())
    if not isinstance(manifest, dict):
        raise RuntimeError("Saleor loadgen ground truth must contain a mapping")
    _install_async_bundle_files()
    sidecar = _load_python_module("saleor_loadgen_sidecar", Path("/app/loadgen_sidecar.py"))
    _install_async_metric_allowlist(sidecar, manifest)
    _install_task_protected_collector(sidecar, manifest)
    _install_protected_content_collector(sidecar)
    sidecar.main()


if __name__ == "__main__":
    main()

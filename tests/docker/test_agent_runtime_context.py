import ast
import logging
import os
import runpy
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
BUILDER = runpy.run_path(str(ROOT / "docker/agents/runtime_context.py"))


def test_public_runtime_has_no_benchmark_identifiers_or_private_grader_sources():
    sources = BUILDER["public_sources"](ROOT)
    assert "incident_runtime/service/kubectl.py" in sources
    assert "clients/codex/driver.py" in sources
    assert "install-scripts/install-codex.sh" in sources
    for name, data in sources.items():
        assert "sregym" not in name.lower()
        assert b"sregym" not in data.lower(), name
        assert not any(part in name.split("/") for part in ("oracles", "problems", "generators", "tests"))
        assert not name.endswith((".md", ".pyc"))
        if name.endswith(".py"):
            ast.parse(data, filename=name)
    identity = sources["clients/harness/problem_id.py"].decode()
    assert 'HARNESS_ARTIFACT_ID_ENV = "RUN_ARTIFACT_ID"' in identity
    assert 'HARNESS_PROBLEM_ID_ENV = "PROBLEM_ID"' in identity
    assert "llm_backend/judge_bridge.py" not in sources
    assert "FAULT_SCRIPTS" not in sources["incident_runtime/paths.py"].decode()


def test_runtime_identity_covers_sources_and_immutable_dependency(tmp_path):
    base = "example.test/dependencies@sha256:" + "1" * 64
    first = BUILDER["write_context"](ROOT, tmp_path / "first", base_image=base)
    second = BUILDER["write_context"](ROOT, tmp_path / "second", base_image=base)
    other_base = "example.test/dependencies@sha256:" + "2" * 64
    other = BUILDER["write_context"](ROOT, tmp_path / "other", base_image=other_base)
    assert first == second and first != other
    dockerfile = (tmp_path / "first/Dockerfile").read_text()
    assert "ENV PYTHONPATH=/opt/runtime" in dockerfile
    assert "RUN rm -rf /opt/sregym" in dockerfile
    assert dockerfile.startswith(f"FROM {base}\n")
    assert "ARG BASE_IMAGE" not in dockerfile
    assert (tmp_path / "other/Dockerfile").read_text().startswith(f"FROM {other_base}\n")
    assert not (tmp_path / "first/runtime/incident_runtime/conductor").exists()


def test_public_runtime_rejects_symlinks_to_private_sources(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    private = tmp_path / "private.py"
    private.write_text("private_grader = True")
    target = root / BUILDER["PUBLIC_MODULES"][0]
    target.parent.mkdir()
    try:
        target.symlink_to(private)
    except OSError:
        pytest.skip("Creating symlinks requires local privileges")
    with pytest.raises(ValueError, match="escapes the checkout"):
        BUILDER["public_sources"](root)


@pytest.mark.parametrize(
    "base",
    [
        "dependencies:latest",
        "dependencies@sha256:111",
        "sha256:" + "1" * 64,
        "Uppercase/repository@sha256:" + "1" * 64,
        "sha256:" + "g" * 64,
        "dependencies@sha256:" + "1" * 64 + "\nRUN false",
    ],
)
def test_runtime_context_rejects_unbound_or_injected_base(tmp_path, base):
    target = tmp_path / "context"
    with pytest.raises(ValueError, match="immutable"):
        BUILDER["write_context"](ROOT, target, base_image=base)
    assert not target.exists()


def test_runtime_context_rejects_stale_private_files_and_existing_empty_directories(tmp_path):
    for name in ("stale", "empty"):
        target = tmp_path / name
        target.mkdir()
        if name == "stale":
            sentinel = target / "runtime/oracles/private.py"
            sentinel.parent.mkdir(parents=True)
            sentinel.write_text("private_verdict = True")
        with pytest.raises(FileExistsError):
            BUILDER["write_context"](ROOT, target, base_image="example.test/dependencies@sha256:" + "1" * 64)
        assert not (target / "Dockerfile").exists()
    assert sentinel.read_text() == "private_verdict = True"


@pytest.mark.parametrize("indirect", [False, True])
def test_runtime_context_rejects_destination_symlink_ancestry(tmp_path, indirect):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks requires local privileges")
    target = link / "context" if indirect else link
    with pytest.raises(ValueError, match="symlink ancestry"):
        BUILDER["write_context"](ROOT, target, base_image="example.test/dependencies@sha256:" + "1" * 64)
    assert not list(outside.iterdir())


@pytest.mark.parametrize("client", ["clients/demo/driver.py", "clients/stratus/stratus_utils/str_to_tool.py"])
def test_generated_client_sessions_reach_unchanged_server(client):
    from yarl import URL

    sources = BUILDER["public_sources"](ROOT)
    helper = {}
    exec(sources["incident_runtime/session.py"], helper)
    call = next(
        node
        for node in ast.walk(ast.parse(sources[client]))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "SSETransport"
    )
    server_tree = ast.parse((ROOT / "mcp_server/kubectl_mcp_tools.py").read_text())
    extractor = next(
        node for node in server_tree.body if isinstance(node, ast.FunctionDef) and node.name == "extract_session_id"
    )
    server = {"Context": object, "URL": URL, "logger": logging.getLogger("session-test")}
    exec(compile(ast.Module(body=[extractor], type_ignores=[]), "actual-session-extractor", "exec"), server)
    for session_id in ("one +/?&", "two"):
        values = {
            "SSETransport": lambda **kwargs: kwargs,
            "_public_session_url": helper["session_url"],
            "session_id": session_id,
            "sse_timeout": 3600,
            "ltc": SimpleNamespace(kubectl_mcp_url="http://localhost/kubectl/sse?existing=&session_id=old#fragment"),
            "mcp_base_url": "http://localhost",
        }
        transport = eval(compile(ast.Expression(body=call), "generated-client-call", "eval"), values)
        request = SimpleNamespace(headers=transport.get("headers", {}), url=transport["url"])
        ctx = SimpleNamespace(request_context=SimpleNamespace(request=request))
        assert server["extract_session_id"](ctx) == session_id
        assert "incident_runtime_ssid" not in request.headers
        assert URL(request.url).query.getall("session_id") == [session_id]
        assert transport["sse_read_timeout"] == 3600
        if client.startswith("clients/demo"):
            assert URL(request.url).query["existing"] == ""
            assert URL(request.url).fragment == "fragment"


def test_session_rewrite_preserves_other_headers_and_future_imports():
    text = '''"""Client."""
from __future__ import annotations
transport = SSETransport(url=url, headers={"sregym_ssid": session_id, "Authorization": token})
'''
    output = BUILDER["_public_session_transport"](text)
    compile(output, "generated", "exec")
    namespace = {
        "url": "http://localhost/sse",
        "session_id": "one",
        "token": "opaque",
        "SSETransport": lambda **kwargs: kwargs,
    }
    tree = ast.parse(output)
    tree.body = [node for node in tree.body if not isinstance(node, ast.ImportFrom)]
    helper = {}
    exec(BUILDER["SESSION_HELPER"], helper)
    namespace["_public_session_url"] = helper["session_url"]
    exec(compile(tree, "generated", "exec"), namespace)
    assert namespace["transport"]["headers"] == {"Authorization": "opaque"}


def test_unrecognized_session_protocol_fails_instead_of_renaming_it():
    with pytest.raises(ValueError, match="Unrecognized"):
        BUILDER["_public_session_transport"]('headers = {"sregym_ssid": session_id}')


@pytest.mark.skipif(os.name != "nt", reason="Windows directory junction contract")
def test_runtime_context_rejects_windows_junction_ancestry(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "junction"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True)
    assert link.is_junction() and not link.is_symlink()
    with pytest.raises(ValueError, match="symlink ancestry"):
        BUILDER["write_context"](ROOT, link / "context", base_image="example.test/dependencies@sha256:" + "1" * 64)
    assert not list(outside.iterdir())


def test_public_runtime_excludes_hidden_client_data(tmp_path):
    root = tmp_path / "checkout"
    for name in BUILDER["PUBLIC_MODULES"]:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("")
    for directory in (".agents", ".runtime", "__pycache__"):
        private = root / "clients" / directory / "private.py"
        private.parent.mkdir(parents=True, exist_ok=True)
        private.write_text("expected_answer = 'private'")
    sources = BUILDER["public_sources"](root)
    assert not any(name.startswith("clients/") for name in sources)


def test_public_runtime_rejects_symlinked_package_directory(tmp_path):
    root = tmp_path / "checkout"
    private = root / "private"
    private.mkdir(parents=True)
    (private / "__init__.py").write_text("expected_answer = 'private'")
    try:
        (root / "sregym").symlink_to(private, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks requires local privileges")
    with pytest.raises(ValueError, match="escapes the checkout"):
        BUILDER["public_sources"](root)

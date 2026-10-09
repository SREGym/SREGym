import ast
import runpy
from pathlib import Path

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
    base = "example.test/dependencies@sha256:111"
    first = BUILDER["write_context"](ROOT, tmp_path / "first", base_image=base)
    second = BUILDER["write_context"](ROOT, tmp_path / "second", base_image=base)
    other = BUILDER["write_context"](ROOT, tmp_path / "other", base_image=base + "2")
    assert first == second and first != other
    dockerfile = (tmp_path / "first/Dockerfile").read_text()
    assert "ENV PYTHONPATH=/opt/runtime" in dockerfile
    assert "RUN rm -rf /opt/sregym" in dockerfile
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

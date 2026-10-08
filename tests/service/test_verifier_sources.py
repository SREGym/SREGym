"""Trusted verifier packaging works without Git and never discovers extra files."""

import hashlib
import json
import subprocess

import pytest

from sregym.service.verifier_sources import MANIFEST_NAME, REQUIRED_SOURCES, source_files, write_source_context


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    for name in sorted(
        REQUIRED_SOURCES | {"sregym/application.py", "sregym/a.py", "sregym/a/b.py", "SREGym-applications/README.md"}
    ):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"source: {name}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
    (root / "sregym/private-token.txt").write_text("untracked credential", encoding="utf-8")
    return root


@pytest.fixture
def packaged(checkout, tmp_path):
    destination = tmp_path / "image"
    write_source_context(checkout, destination)
    return destination


def test_gitless_package_contains_only_allowlisted_bytes_and_ignores_extra_runtime_files(checkout, packaged):
    expected = {path.relative_to(checkout).as_posix(): path.read_bytes() for path in source_files(checkout)}
    assert not (packaged / ".git").exists()
    assert not (packaged / "sregym/private-token.txt").exists()
    # Ordinary DinD COPY may contain unrelated local files. They must not enter
    # the nested verifier context merely because Git metadata is unavailable.
    (packaged / "sregym/untracked-runtime-key").write_text("another secret", encoding="utf-8")
    actual = {path.relative_to(packaged).as_posix(): path.read_bytes() for path in source_files(packaged)}
    assert actual == expected


def test_git_and_packaged_source_order_produces_the_same_build_context(checkout, packaged):
    # Path ordering compares path components; string ordering compares the
    # slash and dot too. Both sources must use the same platform-neutral order.
    expected = [path.relative_to(checkout).as_posix() for path in source_files(checkout)]
    actual = [path.relative_to(packaged).as_posix() for path in source_files(packaged)]
    assert actual == expected == sorted(expected)
    assert expected.index("sregym/a.py") < expected.index("sregym/a/b.py")


@pytest.mark.parametrize("change", ["modify", "delete"])
def test_packaged_source_changes_fail_closed(packaged, change):
    path = packaged / "sregym/application.py"
    if change == "delete":
        path.unlink()
    else:
        path.write_text("modified grader", encoding="utf-8")
    with pytest.raises(ValueError, match="(?i)packaged verifier source"):
        source_files(packaged)


@pytest.mark.parametrize("name", ["../outside", "sregym/../../outside", "sregym/\\outside", "/tmp/outside", ".env"])
def test_manifest_cannot_select_paths_outside_its_source_allowlist(packaged, name):
    manifest = packaged / MANIFEST_NAME
    metadata = json.loads(manifest.read_text(encoding="utf-8"))
    metadata["files"][name] = hashlib.sha256(b"outside").hexdigest()
    manifest.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid packaged verifier source path"):
        source_files(packaged)


def test_manifest_cannot_omit_required_verifier_code(packaged):
    manifest = packaged / MANIFEST_NAME
    metadata = json.loads(manifest.read_text(encoding="utf-8"))
    metadata["files"].pop("sregym/service/verifier_worker.py")
    manifest.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ValueError, match="missing required files"):
        source_files(packaged)


def test_git_worktree_does_not_trust_an_untracked_packaged_manifest(checkout):
    (checkout / MANIFEST_NAME).write_text("not a trusted manifest", encoding="utf-8")
    names = {path.relative_to(checkout).as_posix() for path in source_files(checkout)}
    assert "sregym/private-token.txt" not in names
    assert names >= REQUIRED_SOURCES


def test_gitless_runtime_without_manifest_has_no_recursive_fallback(tmp_path):
    (tmp_path / "private-token.txt").write_text("secret", encoding="utf-8")
    with pytest.raises(ValueError, match="trusted packaged manifest"):
        source_files(tmp_path)


@pytest.mark.parametrize("replace_manifest", [False, True])
def test_packaged_sources_reject_symlinks_even_when_contents_match(packaged, tmp_path, replace_manifest):
    path = packaged / MANIFEST_NAME if replace_manifest else packaged / "sregym/application.py"
    outside = tmp_path / "outside"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    try:
        path.symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks is unavailable on this host")
    with pytest.raises(ValueError, match="(?:regular file|Invalid packaged verifier source path)"):
        source_files(packaged)


def test_packaging_refuses_to_replace_an_existing_context(checkout, tmp_path):
    destination = tmp_path / "context"
    destination.mkdir()
    existing = destination / "do-not-replace"
    existing.write_text("owned by another build", encoding="utf-8")
    with pytest.raises(ValueError, match="must be empty"):
        write_source_context(checkout, destination)
    assert existing.read_text(encoding="utf-8") == "owned by another build"

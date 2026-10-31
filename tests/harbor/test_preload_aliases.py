"""Canonical image aliases must be verified through the node's actual CRI."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("preload_aliases", ROOT / "docker/userns-k3s/preload-aliases.py")
aliases = importlib.util.module_from_spec(spec)
spec.loader.exec_module(aliases)
DIGEST = "sha256:" + "a" * 64
SOURCE = "example.test/app@" + DIGEST
TAG = "example.test/app:sregym-preload-" + "a" * 64


def manifest(role="workers"):
    return {"images": {SOURCE: {"aliases": [SOURCE], "import_tags": [TAG], "role": role}}}


def test_restore_waits_for_cri_lookup_after_adding_canonical_alias(monkeypatch):
    targets = {TAG: DIGEST}
    lookups = []
    tagged = []

    def command(arguments):
        code, output = 0, ""
        if arguments[-2:] == ["images", "ls"]:
            output = "REF TYPE DIGEST SIZE\n" + "\n".join(
                f"{name} image/index {digest} 1KB" for name, digest in targets.items()
            )
        elif "tag" in arguments:
            tagged.append(arguments[-1])
            targets[arguments[-1]] = targets[arguments[-2]]
        elif "inspecti" in arguments:
            lookups.append(arguments[-1])
            code = 1 if len(lookups) == 1 else 0
        return subprocess.CompletedProcess(arguments, code, output, "")

    monkeypatch.setattr(aliases, "command", command)
    monkeypatch.setattr(aliases.time, "sleep", lambda _: None)
    aliases.restore(manifest(), "agent", 10)
    assert tagged == [SOURCE]
    assert lookups == [SOURCE, SOURCE]
    assert targets[SOURCE] == DIGEST


def test_restore_rejects_imported_tag_with_different_target(monkeypatch):
    def command(arguments):
        assert arguments[-2:] == ["images", "ls"]
        return subprocess.CompletedProcess(arguments, 0, f"{TAG} image/index sha256:{'b' * 64} 1KB", "")

    monkeypatch.setattr(aliases, "command", command)
    with pytest.raises(RuntimeError, match="target changed"):
        aliases.restore(manifest(), "agent", 10)


def test_server_never_restores_worker_only_images(monkeypatch):
    monkeypatch.setattr(aliases, "command", lambda _: pytest.fail("server touched worker-only image"))
    aliases.restore(manifest(), "server", 10)


def test_wait_propagates_node_failure_instead_of_allowing_network_fallback(tmp_path):
    path = tmp_path / "kind-worker/k3s" / aliases.STATUS
    path.parent.mkdir(parents=True)
    aliases.write_status(path, "failed", "digest changed")
    with pytest.raises(RuntimeError, match="kind-worker image preload failed: digest changed"):
        aliases.wait(tmp_path, ["kind-worker"], 10)
    assert json.loads(path.read_text())["state"] == "failed"


def test_wait_requires_every_node_to_verify_its_cache(tmp_path):
    for node in aliases.NODES:
        path = tmp_path / node / "k3s" / aliases.STATUS
        path.parent.mkdir(parents=True)
        aliases.write_status(path, "ready")
    aliases.wait(tmp_path, aliases.NODES, 10)

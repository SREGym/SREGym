"""Integrity checks for the runner-owned standalone oracle snapshot."""

import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.service import verifier_runtime, verifier_worker

pytestmark = pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="Requires Linux no-follow snapshot access")


@pytest.fixture
def snapshot_runner(monkeypatch):
    source = Path(__file__).resolve().parents[2] / "run-oracle.py"
    spec = importlib.util.spec_from_file_location("verifier_snapshot_cli_test", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    runtime = Mock()
    runtime.evaluate_snapshot.return_value = {"success": True}
    monkeypatch.setattr(verifier_runtime, "VerifierRuntime", lambda: runtime)
    monkeypatch.setattr(verifier_worker, "MAX_FRAME_BYTES", 32)
    return module.run_oracle_from_snapshot, runtime


def _private_snapshot(tmp_path, payload=b"trusted snapshot"):
    path = tmp_path / "oracle.pickle"
    path.write_bytes(payload)
    path.chmod(0o600)
    return path


def test_snapshot_reads_the_validated_descriptor_after_path_replacement(tmp_path, monkeypatch, snapshot_runner):
    run, runtime = snapshot_runner
    path = _private_snapshot(tmp_path)
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"substituted payload")
    replacement.chmod(0o600)
    fstat = os.fstat

    def replace_after_validation(descriptor):
        metadata = fstat(descriptor)
        os.replace(replacement, path)
        return metadata

    monkeypatch.setattr(os, "fstat", replace_after_validation)
    assert run(path) == {"success": True}
    runtime.evaluate_snapshot.assert_called_once_with(b"trusted snapshot")
    assert path.read_bytes() == b"substituted payload"


def test_snapshot_rejects_a_symlink(tmp_path, snapshot_runner):
    run, runtime = snapshot_runner
    path = _private_snapshot(tmp_path)
    link = tmp_path / "snapshot-link"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="must not be a symlink"):
        run(link)
    runtime.evaluate_snapshot.assert_not_called()


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o660, 0o606])
def test_snapshot_rejects_group_or_other_permissions(tmp_path, snapshot_runner, mode):
    run, runtime = snapshot_runner
    path = _private_snapshot(tmp_path)
    path.chmod(mode)
    with pytest.raises(ValueError, match="must be private"):
        run(path)
    runtime.evaluate_snapshot.assert_not_called()


def test_snapshot_rejects_a_foreign_owner(tmp_path, monkeypatch, snapshot_runner):
    run, runtime = snapshot_runner
    path = _private_snapshot(tmp_path)
    fstat = os.fstat

    def foreign_owner(descriptor):
        metadata = fstat(descriptor)
        return SimpleNamespace(st_mode=metadata.st_mode, st_size=metadata.st_size, st_uid=os.getuid() + 1)

    monkeypatch.setattr(os, "fstat", foreign_owner)
    with pytest.raises(ValueError, match="must belong to the runner"):
        run(path)
    runtime.evaluate_snapshot.assert_not_called()


@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_snapshot_rejects_nonregular_files_without_waiting_for_a_writer(tmp_path, snapshot_runner, kind):
    run, runtime = snapshot_runner
    path = tmp_path / "not-a-snapshot"
    if kind == "directory":
        path.mkdir(mode=0o700)
    else:
        os.mkfifo(path, mode=0o600)
    with pytest.raises(ValueError, match="must be a regular file"):
        run(path)
    runtime.evaluate_snapshot.assert_not_called()


def test_snapshot_accepts_the_size_limit(tmp_path, snapshot_runner):
    run, runtime = snapshot_runner
    path = _private_snapshot(tmp_path, b"a" * 32)
    assert run(path) == {"success": True}
    runtime.evaluate_snapshot.assert_called_once_with(b"a" * 32)


def test_snapshot_rejects_an_oversized_file(tmp_path, snapshot_runner):
    run, runtime = snapshot_runner
    path = _private_snapshot(tmp_path, b"a" * 33)
    with pytest.raises(ValueError, match="input size limit"):
        run(path)
    runtime.evaluate_snapshot.assert_not_called()


def test_snapshot_read_is_bounded_if_file_grows_after_metadata_validation(tmp_path, monkeypatch, snapshot_runner):
    run, runtime = snapshot_runner
    path = _private_snapshot(tmp_path)
    fstat = os.fstat

    def grow_after_validation(descriptor):
        metadata = fstat(descriptor)
        with path.open("ab") as output:
            output.write(b"a" * 64)
        return metadata

    monkeypatch.setattr(os, "fstat", grow_after_validation)
    with pytest.raises(ValueError, match="input size limit"):
        run(path)
    runtime.evaluate_snapshot.assert_not_called()

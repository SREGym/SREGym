import importlib.util
import json
import os

import pytest

from sregym.chaos.artifact import discover_faults


@pytest.fixture
def exporter(tmp_path, monkeypatch):
    artifact = discover_faults()["profile_address_encryption_mismatch_hotel_reservation"]
    spec = importlib.util.spec_from_file_location("contact_export", artifact.path / "contact_sync.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "KEYFILE", str(tmp_path / "key"))
    monkeypatch.setattr(module, "profiles", lambda: [])
    return module


def test_missing_key_cannot_silently_rotate_or_change_data(exporter, monkeypatch):
    monkeypatch.setattr(exporter, "update", lambda _: pytest.fail("catalog must remain unchanged"))
    with pytest.raises(FileNotFoundError):
        exporter.migrate()
    with pytest.raises(FileNotFoundError):
        exporter.getkey()


@pytest.mark.parametrize("size", [0, 16, 31, 33, 64])
def test_invalid_key_is_rejected_before_migration(exporter, monkeypatch, size):
    from pathlib import Path

    Path(exporter.KEYFILE).write_bytes(b"x" * size)
    monkeypatch.setattr(exporter, "update", lambda _: pytest.fail("catalog must remain unchanged"))
    with pytest.raises(ValueError, match="32 bytes"):
        exporter.migrate()


def test_decryption_preserves_non_seed_address(exporter):
    address = {"streetNumber": "818", "streetName": "Lifecycle Avenue", "postalCode": "61234"}
    key = bytes(range(32))
    assert exporter.unseal(exporter.seal(address, key), key) == address
    with pytest.raises(ValueError, match="authentication"):
        exporter.unseal(exporter.seal(address, key), b"z" * 32)


@pytest.mark.parametrize("matching_catalog", [True, False])
def test_cleanup_uses_a_private_snapshot_only_for_the_original_catalog(tmp_path, monkeypatch, matching_catalog):
    workspace = discover_faults()["profile_address_encryption_mismatch_hotel_reservation"].workspace()
    (workspace.path / "original.bson64").write_text("private-snapshot")
    (workspace.path / "store.uid").write_text("catalog-uid" if matching_catalog else "another-catalog")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.jsonl"
    kubectl = bin_dir / "kubectl"
    kubectl.write_text(
        """#!/usr/bin/env python3
import json,os,sys
args=sys.argv[3:]
with open(os.environ['KUBECTL_CALL_LOG'],'a') as log:
    log.write(json.dumps(args)+'\\n')
if args[:2]==['get','pods']:
    print('publisher-pod')
elif args[:2]==['get','pvc']:
    print('catalog-uid')
elif args[0]=='exec':
    sys.stdin.read()
elif args[:2]==['get','secret']:
    if any('jsonpath' in arg for arg in args):
        sys.exit(1)  # The operational key was deleted.
elif args[:2]!=['get','configmap']:
    raise AssertionError(args)
"""
    )
    kubectl.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("KUBECTL_CALL_LOG", str(calls))
    result = workspace.run("recover.sh", "hotel-reservation")
    recorded = [json.loads(line) for line in calls.read_text().splitlines()]
    assert (result.returncode == 0) == matching_catalog, result.stderr
    assert any("restore-snapshot" in call for call in recorded) == matching_catalog
    if matching_catalog:
        assert not any(call[:2] == ["get", "secret"] and any("jsonpath" in arg for arg in call) for call in recorded)

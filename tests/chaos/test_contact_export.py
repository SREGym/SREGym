import importlib.util

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

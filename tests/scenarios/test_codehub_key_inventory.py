"""Exact trusted snapshot codec corruption and frame-bound controls."""

import hashlib
import pickle
import struct
import zlib
from dataclasses import replace
from uuid import UUID

import pytest

from sregym.conductor.oracles.codehub_state import ProtectedReceiptCut
from sregym.conductor.scenarios.codehub_key_inventory import (
    HEADER,
    MAX_KEYS,
    EncodedKeyInventory,
    decode_key_inventory,
    encode_key_inventory,
    register_cut_codec,
    validate_cut_snapshot_budget,
)


def uid(value):
    return str(UUID(int=value))


def keys(count=12, tenants=3):
    return tuple(
        sorted(f"{uid(tenant + 1)}/{uid(index + 100)}" for index in range(count) for tenant in [index % tenants])
    )


def cut(inventory):
    return ProtectedReceiptCut("group-a", (0, 2), len(inventory) + 1, inventory, "a" * 64, "b" * 64)


def rewritten(encoded, mutate):
    raw = bytearray(zlib.decompress(encoded.payload))
    mutate(raw)
    return replace(encoded, payload=zlib.compress(raw), sha256=hashlib.sha256(raw).hexdigest())


def test_exact_uuid_codec_roundtrips_every_key_and_has_no_path_or_endpoint():
    inventory = keys(2000, 16)
    encoded = encode_key_inventory(inventory)
    assert decode_key_inventory(encoded) == inventory
    assert encoded.count == 2000 and encoded.tenants == 16
    assert encoded.expanded_bytes == HEADER.size + 20 * 16 + 16 * 2000
    assert not hasattr(encoded, "path") and not hasattr(encoded, "endpoint")
    assert "payload=" not in repr(encoded)


@pytest.mark.parametrize(
    "inventory",
    [
        (),
        list(keys()),
        keys() + (keys()[-1],),
        tuple(reversed(keys())),
        ("not/a-uuid",),
        (f"{str(UUID(int=0xABC)).upper()}/{uid(100)}",),
    ],
)
def test_encoder_rejects_empty_mutable_duplicate_unordered_and_malformed_keys(inventory):
    with pytest.raises(ValueError):
        encode_key_inventory(inventory)


@pytest.mark.parametrize(
    "change",
    [
        lambda value: replace(value, count=True),
        lambda value: replace(value, count=MAX_KEYS + 1),
        lambda value: replace(value, tenants=True),
        lambda value: replace(value, expanded_bytes=value.expanded_bytes + 1),
        lambda value: replace(value, version=2),
        lambda value: replace(value, sha256="not-a-digest"),
        lambda value: replace(value, payload=bytearray(value.payload)),
    ],
)
def test_declared_counts_types_versions_and_expansion_bounds_are_strict(change):
    with pytest.raises(ValueError):
        change(encode_key_inventory(keys()))


def test_content_digest_truncation_and_trailing_compressed_data_are_rejected():
    encoded = encode_key_inventory(keys())
    for invalid in (
        replace(encoded, sha256="c" * 64),
        replace(encoded, payload=encoded.payload[:-1]),
        replace(encoded, payload=encoded.payload + b"trailing"),
        replace(encoded, payload=encoded.payload + encoded.payload),
    ):
        with pytest.raises(ValueError):
            decode_key_inventory(invalid)


def test_compressed_oversized_expansion_fails_at_the_declared_bound():
    encoded = encode_key_inventory(keys(1, 1))
    invalid = replace(encoded, payload=zlib.compress(b"x" * (1024 * 1024)))
    with pytest.raises(ValueError, match="expansion"):
        decode_key_inventory(invalid)


def test_header_counts_cannot_disagree_with_the_typed_envelope():
    encoded = encode_key_inventory(keys())
    invalid = rewritten(encoded, lambda raw: struct.pack_into(">I", raw, 5, encoded.count + 1))
    with pytest.raises(ValueError, match="header"):
        decode_key_inventory(invalid)


def test_duplicate_entities_are_rejected_even_with_a_matching_content_digest():
    encoded = encode_key_inventory(keys(2, 1))
    start = HEADER.size + 20
    invalid = rewritten(encoded, lambda raw: raw.__setitem__(slice(start + 16, start + 32), raw[start : start + 16]))
    with pytest.raises(ValueError, match="duplicates or invalid order"):
        decode_key_inventory(invalid)


def test_tenant_dictionary_order_is_checked_independently_of_the_digest():
    encoded = encode_key_inventory(keys(2, 2))
    invalid = rewritten(
        encoded,
        lambda raw: raw.__setitem__(slice(HEADER.size + 36, HEADER.size + 52), raw[HEADER.size : HEADER.size + 16]),
    )
    with pytest.raises(ValueError, match="tenant dictionary"):
        decode_key_inventory(invalid)


def test_trusted_pickle_reducer_preserves_legacy_cut_constructor_and_shared_identity():
    register_cut_codec()
    original = cut(keys(4000, 16))
    payload = pickle.dumps((original, original), protocol=pickle.HIGHEST_PROTOCOL)
    decoded = pickle.loads(payload)
    assert decoded == (original, original) and decoded[0] is decoded[1]
    assert type(decoded[0].record_keys) is tuple
    assert len(payload) < sum(len(key) for key in original.record_keys)


def test_cloudpickle_instance_dispatch_uses_the_registered_exact_reducer():
    cloudpickle = pytest.importorskip("cloudpickle")
    register_cut_codec()
    original = cut(keys(1000, 8))
    decoded = cloudpickle.loads(cloudpickle.dumps(original))
    assert decoded == original and type(decoded.record_keys) is tuple


def test_frame_budget_counts_independent_cuts_once_each_and_shared_cuts_once_total():
    first, second = cut(keys(4000)), cut(keys(4000))
    one = validate_cut_snapshot_budget((first,), max_frame_bytes=512 * 1024, reserved_bytes=0)
    assert validate_cut_snapshot_budget((first, first), max_frame_bytes=512 * 1024, reserved_bytes=0) == one
    with pytest.raises(ValueError, match="frame budget"):
        validate_cut_snapshot_budget((first, second), max_frame_bytes=512 * 1024, reserved_bytes=0)


def test_unknown_codec_types_cannot_bypass_validation():
    with pytest.raises(ValueError):
        decode_key_inventory({"payload": b"unknown"})
    with pytest.raises(ValueError):
        EncodedKeyInventory(1, 1, 1, 999, "a" * 64, b"unknown")


def test_oracle_serialization_rejects_an_inventory_that_exceeds_its_private_frame(monkeypatch):
    from sregym.conductor.oracles.regional_database_recovery import RegionalDatabaseRecoveryOracle

    first, second = cut(keys(4000)), cut(keys(4000))
    value = RegionalDatabaseRecoveryOracle(None, cuts=(second,))
    value.baseline_cuts = (first,)
    monkeypatch.setattr(
        "sregym.conductor.oracles.regional_database_recovery.validate_cut_snapshot_budget",
        lambda inventories: validate_cut_snapshot_budget(inventories, max_frame_bytes=512 * 1024, reserved_bytes=0),
    )
    with pytest.raises(ValueError, match="frame budget"):
        pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)

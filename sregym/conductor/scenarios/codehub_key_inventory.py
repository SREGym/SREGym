"""Bounded exact UUID inventories for runner-owned verifier snapshots.

The codec contains every protected key. It is neither approximate membership nor
a workload-visible artifact, and it accepts no paths or remote data sources.
"""

import copyreg
import hashlib
import re
import struct
import zlib
from dataclasses import dataclass, field

from sregym.conductor.oracles.codehub_state import ProtectedReceiptCut

VERSION = 1
MAGIC = b"CHKI"
HEADER = struct.Struct(">4sBII")
COUNT = struct.Struct(">I")
MAX_KEYS = 2_000_000
MAX_TENANTS = 4096
MAX_EXPANDED_BYTES = HEADER.size + 20 * MAX_TENANTS + 16 * MAX_KEYS
MAX_COMPRESSED_OVERHEAD = 64 * 1024
MAX_COMPRESSED_BYTES = MAX_EXPANDED_BYTES + MAX_COMPRESSED_OVERHEAD
MAX_FRAME_BYTES = 64 * 1024 * 1024
NON_INVENTORY_FRAME_RESERVE = 1024 * 1024
CHUNK_BYTES = 64 * 1024
UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@dataclass(frozen=True)
class EncodedKeyInventory:
    version: int
    count: int
    tenants: int
    expanded_bytes: int
    sha256: str
    payload: bytes = field(repr=False)

    def __post_init__(self):
        if type(self.version) is not int or self.version != VERSION:
            raise ValueError("Unsupported exact key inventory version")
        if type(self.count) is not int or not 1 <= self.count <= MAX_KEYS:
            raise ValueError("Exact key count exceeds its configured bound")
        if type(self.tenants) is not int or not 1 <= self.tenants <= min(self.count, MAX_TENANTS):
            raise ValueError("Exact tenant count exceeds its configured bound")
        expected = HEADER.size + 20 * self.tenants + 16 * self.count
        if type(self.expanded_bytes) is not int or self.expanded_bytes != expected or expected > MAX_EXPANDED_BYTES:
            raise ValueError("Declared key inventory expansion is invalid")
        if type(self.sha256) is not str or not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise ValueError("Exact inventory requires an independent content digest")
        if type(self.payload) is not bytes or not self.payload or len(self.payload) > MAX_COMPRESSED_BYTES:
            raise ValueError("Compressed key inventory exceeds its configured bound")


def _groups(keys):
    if type(keys) is not tuple or not 1 <= len(keys) <= MAX_KEYS:
        raise ValueError("Expected a bounded nonempty exact key tuple")
    groups, previous = [], None
    for key in keys:
        if (
            type(key) is not str
            or len(key) != 73
            or key[36] != "/"
            or not UUID_PATTERN.fullmatch(key[:36])
            or not UUID_PATTERN.fullmatch(key[37:])
        ):
            raise ValueError("Exact inventory keys must be canonical tenant/entity UUIDs")
        if previous is not None and key <= previous:
            raise ValueError("Exact inventory cannot contain duplicates or unordered keys")
        tenant = key[:36]
        if not groups or groups[-1][0] != tenant:
            if len(groups) == MAX_TENANTS:
                raise ValueError("Exact tenant dictionary exceeds its configured bound")
            groups.append([tenant, 1])
        else:
            groups[-1][1] += 1
        previous = key
    return groups


def encode_key_inventory(keys):
    groups = _groups(keys)
    checksum, compressor, output = hashlib.sha256(), zlib.compressobj(level=6), []

    def emit(content):
        checksum.update(content)
        compressed = compressor.compress(content)
        if compressed:
            output.append(compressed)

    emit(HEADER.pack(MAGIC, VERSION, len(keys), len(groups)))
    offset = 0
    for tenant, count in groups:
        emit(bytes.fromhex(tenant.replace("-", "")) + COUNT.pack(count))
        pending = bytearray()
        for index in range(offset, offset + count):
            key = keys[index]
            pending.extend(bytes.fromhex(key[37:].replace("-", "")))
            if len(pending) == CHUNK_BYTES:
                emit(pending)
                pending.clear()
        if pending:
            emit(pending)
        offset += count
    output.append(compressor.flush())
    payload = b"".join(output)
    return EncodedKeyInventory(
        VERSION, len(keys), len(groups), HEADER.size + 20 * len(groups) + 16 * len(keys), checksum.hexdigest(), payload
    )


def _inflate(encoded):
    decoder, checksum, expanded = zlib.decompressobj(), hashlib.sha256(), 0
    try:
        for offset in range(0, len(encoded.payload), CHUNK_BYTES):
            pending = encoded.payload[offset : offset + CHUNK_BYTES]
            while pending:
                content = decoder.decompress(pending, CHUNK_BYTES)
                pending = decoder.unconsumed_tail
                expanded += len(content)
                if expanded > encoded.expanded_bytes or decoder.unused_data:
                    raise ValueError("Exact key inventory exceeds its expansion or has trailing data")
                checksum.update(content)
                if content:
                    yield content
        if not decoder.eof or expanded != encoded.expanded_bytes or checksum.hexdigest() != encoded.sha256:
            raise ValueError("Truncated or mismatched exact key inventory")
    except zlib.error as exc:
        raise ValueError("Malformed exact key inventory compression") from exc


class _Reader:
    def __init__(self, chunks):
        self.chunks, self.current, self.offset = iter(chunks), b"", 0

    def take(self, count):
        if len(self.current) - self.offset >= count:
            value = self.current[self.offset : self.offset + count]
            self.offset += count
            return value
        parts = [self.current[self.offset :]]
        remaining = count - len(parts[0])
        while remaining:
            try:
                self.current = next(self.chunks)
            except StopIteration as exc:
                raise ValueError("Truncated exact key inventory structure") from exc
            self.offset = min(len(self.current), remaining)
            parts.append(self.current[: self.offset])
            remaining -= self.offset
        return b"".join(parts)

    def finish(self):
        if self.offset != len(self.current):
            raise ValueError("Trailing exact key inventory structure")
        try:
            next(self.chunks)
        except StopIteration:
            return
        raise ValueError("Trailing exact key inventory structure")


def _uuid_text(value):
    text = value.hex()
    return f"{text[:8]}-{text[8:12]}-{text[12:16]}-{text[16:20]}-{text[20:]}"


def decode_key_inventory(encoded):
    if type(encoded) is not EncodedKeyInventory:
        raise ValueError("Expected a typed exact key inventory")
    encoded.__post_init__()
    reader = _Reader(_inflate(encoded))
    magic, version, count, tenants = HEADER.unpack(reader.take(HEADER.size))
    if (magic, version, count, tenants) != (MAGIC, encoded.version, encoded.count, encoded.tenants):
        raise ValueError("Declared key inventory header does not match its contents")
    keys, previous_tenant = [], None
    for _index in range(tenants):
        tenant, group_count = reader.take(16), COUNT.unpack(reader.take(4))[0]
        if previous_tenant is not None and tenant <= previous_tenant:
            raise ValueError("Exact tenant dictionary has duplicates or invalid order")
        if not 1 <= group_count <= count - len(keys):
            raise ValueError("Exact tenant group count is invalid")
        tenant_text, previous_entity = _uuid_text(tenant), None
        remaining = group_count
        while remaining:
            entries = min(CHUNK_BYTES // 16, remaining)
            content = reader.take(entries * 16)
            for offset in range(0, len(content), 16):
                entity = content[offset : offset + 16]
                if previous_entity is not None and entity <= previous_entity:
                    raise ValueError("Exact entity inventory has duplicates or invalid order")
                keys.append(f"{tenant_text}/{_uuid_text(entity)}")
                previous_entity = entity
            remaining -= entries
        previous_tenant = tenant
    if len(keys) != count:
        raise ValueError("Exact inventory count differs from the decoded identities")
    reader.finish()
    return tuple(keys)


def restore_protected_cut(group, closed_epochs, operations, journal_sha256, current_sha256, inventory):
    return ProtectedReceiptCut(
        group, closed_epochs, operations, decode_key_inventory(inventory), journal_sha256, current_sha256
    )


def reduce_protected_cut(cut):
    return (
        restore_protected_cut,
        (
            cut.group,
            cut.closed_epochs,
            cut.operations,
            cut.journal_sha256,
            cut.current_sha256,
            encode_key_inventory(cut.record_keys),
        ),
    )


def register_cut_codec():
    # copyreg is the standard trusted-pickle dispatch seam also used by
    # cloudpickle's instance dispatch. No evaluated-agent input is unpickled.
    copyreg.pickle(ProtectedReceiptCut, reduce_protected_cut)


def validate_cut_snapshot_budget(cuts, *, max_frame_bytes=MAX_FRAME_BYTES, reserved_bytes=NON_INVENTORY_FRAME_RESERVE):
    if type(cuts) is not tuple or any(type(cut) is not ProtectedReceiptCut for cut in cuts):
        raise ValueError("Snapshot inventory budget requires immutable protected cuts")
    if (
        type(max_frame_bytes) is not int
        or not 1 <= max_frame_bytes <= MAX_FRAME_BYTES
        or type(reserved_bytes) is not int
        or not 0 <= reserved_bytes < max_frame_bytes
    ):
        raise ValueError("Invalid private verifier snapshot budget")
    seen, binary_upper = set(), 0
    for cut in cuts:
        if id(cut) in seen:
            continue
        seen.add(id(cut))
        count = len(cut.record_keys)
        if not 1 <= count <= MAX_KEYS:
            raise ValueError("Snapshot exact key count exceeds its configured bound")
        # Conservative zlib and pickle metadata allowance avoids admitting a
        # frame based on optimistic compression of random UUID identities.
        binary_upper += HEADER.size + 20 * min(count, MAX_TENANTS) + 16 * count + MAX_COMPRESSED_OVERHEAD + 1024
    frame_upper = 4 * ((binary_upper + 2) // 3) + reserved_bytes
    if frame_upper > max_frame_bytes:
        raise ValueError("Exact protected inventories exceed the private verifier frame budget")
    return frame_upper

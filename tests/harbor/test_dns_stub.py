"""Tests for the userns-k3s cluster's DNS stub (docker/userns-k3s/dns-stub.py)."""

import importlib.util
import struct
from pathlib import Path

STUB = Path(__file__).resolve().parents[2] / "docker/userns-k3s/dns-stub.py"
spec = importlib.util.spec_from_file_location("dns_stub", STUB)
dns_stub = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dns_stub)


def _query(name: str, qtype: int = 28, flags: int = 0x0100, edns: bool = True) -> bytes:
    question = b"".join(bytes([len(label)]) + label.encode() for label in name.split(".")) + b"\0"
    question += struct.pack("!HH", qtype, 1)
    opt = b"\0" + struct.pack("!HHIH", 41, 1232, 0, 0) if edns else b""
    return struct.pack("!HHHHHH", 0xBEEF, flags, 1, 0, 0, 1 if edns else 0) + question + opt


def test_every_name_is_answered_nxdomain_with_its_question():
    query = _query("basic-pd.cluster.local")
    reply = dns_stub.answer(query)
    ident, flags, qd, an, ns, ar = struct.unpack("!HHHHHH", reply[:12])
    assert ident == 0xBEEF
    assert flags & 0x8000 and flags & 0x0100 and flags & 0x0080  # response, RD echoed, RA
    assert flags & 0x000F == dns_stub.NXDOMAIN
    assert (qd, an, ns, ar) == (1, 0, 0, 0)
    # The question is echoed; the client's EDNS record is not.
    assert reply[12:] == query[12 : len(query) - 11]


def test_malformed_queries_and_responses_get_no_reply():
    assert dns_stub.answer(b"\0" * 5) is None
    assert dns_stub.answer(_query("example.com", flags=0x8100)) is None
    assert dns_stub.answer(_query("example.com")[:20]) is None

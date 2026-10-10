#!/usr/bin/env python3
"""Answer every DNS query with NXDOMAIN.

The nodes have no route out, so CoreDNS cannot reach the container's own
resolvers (e.g. 8.8.8.8): every name outside cluster.local waited seconds for
CoreDNS's forward timeout. That includes the tail of a short name's search
list, so a client resolving "basic-pd" for both A and AAAA stalled after the A
answer. gRPC's c-ares resolver gives up after 2s, and TiDB's TiKV never reached
its PD. Started on the fabric bridge as the nodes' upstream, this makes those
lookups fail at once. Pods cannot reach outside the cluster anyway.

Usage: dns-stub.py ADDRESS [PORT]
"""

import socket
import struct
import sys

NXDOMAIN = 3
NOTIMP = 4


def answer(query: bytes) -> bytes | None:
    """The reply to one query: its header and question, no records."""
    if len(query) < 12:
        return None
    ident, flags, qdcount = struct.unpack("!HHH", query[:6])
    if flags & 0x8000 or qdcount != 1:
        return None
    end = 12
    while end < len(query) and query[end]:
        if query[end] & 0xC0:  # questions are not compressed
            return None
        end += query[end] + 1
    end += 5  # root label, QTYPE, QCLASS
    if end > len(query):
        return None
    opcode = flags & 0x7800
    rcode = NXDOMAIN if opcode == 0 else NOTIMP
    # QR, the query's opcode and RD, RA.
    reply_flags = 0x8000 | opcode | (flags & 0x0100) | 0x0080 | rcode
    return struct.pack("!HHHHHH", ident, reply_flags, 1, 0, 0, 0) + query[12:end]


def main() -> None:
    address = sys.argv[1]
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 53
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((address, port))
    while True:
        query, peer = sock.recvfrom(4096)
        reply = answer(query)
        if reply:
            sock.sendto(reply, peer)


if __name__ == "__main__":
    main()

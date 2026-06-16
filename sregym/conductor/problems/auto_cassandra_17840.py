"""CASSANDRA-17840 — IndexOutOfBoundsException in paging-state version inference, on the raw-ring harness.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-17840
Buggy: 4.0.5 (also confirmed buggy in 4.0.6)  ->  Fixed: 4.0.7 (A/B control = cassandra:4.0.7).
  NOTE: the JIRA ``fixVersions`` field lists 4.0.6, but per the reproduction evidence that is wrong — 4.0.6
  still reproduces identically and its source is unchanged. The first fixed 4.0.x release is 4.0.7
  (CHANGES.txt: "Fix potential IndexOutOfBoundsException in PagingState in mixed mode clusters
  (CASSANDRA-17840)").
Component: Messaging/Client.

THE BUG (single node — RF=1 is sufficient; the defect is in coordinator-side paging-state parsing):
  In ``PagingState.isModernSerialized`` (4.0.5/4.0.6 source) ``index`` is an ``int`` and the code does
  ``index += computeUnsignedVIntSize(partitionKeyLen) + partitionKeyLen`` (int += long) with no
  ``Math.toIntExact`` / overflow guard. A CQL native-protocol **v4** QUERY carrying a crafted
  ``paging_state`` whose first unsigned-VInt (partitionKeyLen) decodes to a large positive value
  (0x7FFFFFFF) overflows ``index`` to a negative number (-2147483644), which still passes the
  ``index >= limit`` check; ``getUnsignedVInt`` is then called with that negative index and
  ``input.get(-2147483644)`` throws ``IndexOutOfBoundsException``. Because that is NOT an ``IOException`` it
  escapes ``deserialize()``'s ``catch (IOException)`` and leaks to the client as a SERVER_ERROR
  (opcode 0x00, error code 0x00000000) instead of a clean PROTOCOL_ERROR. ``deserialize()`` on a V4+
  connection calls ``isModernSerialized(bytes)`` first, so a client merely needs to send the crafted
  ``paging_state`` over a V4 QUERY to hit it. The fix (4.0.7) reads partitionKeyLen via ``toIntExact()``
  (throws on overflow) and adds an ``index < 0`` guard (``addNonNegative``).

VERBATIM BUGGY SIGNATURE (the SERVER_ERROR message returned over the native protocol):
  java.lang.IndexOutOfBoundsException: -2147483644

Fixed 4.0.7 returns, for the identical crafted paging_state:
  ERROR code=0x0000000a (PROTOCOL_ERROR) message='Invalid value for the paging state'

HOW THE RAW-RING HARNESS MAKES THIS RUNNABLE (the former blocker, resolved):
  cqlsh cannot inject an arbitrary raw ``paging_state``, so the trigger is a pure-Python stdlib raw
  native-protocol v4 client (``_CLIENT_SRC`` below) staged into the node and run with the in-image
  ``python3``. It performs the STARTUP/READY handshake then sends one QUERY with the
  ``with_paging_state`` flag (0x08) and the crafted state ``f0 7f ff ff ff``, and prints the server's
  ERROR frame verbatim. ``CassandraLogGrepOracle(source='command')`` runs that client on ``cass-0`` and
  greps its output for ``java.lang.IndexOutOfBoundsException: -2147483644``.

Verified end-to-end on kind-fleet3: the crafted v4 QUERY makes 4.0.5 leak
``java.lang.IndexOutOfBoundsException: -2147483644`` (SERVER_ERROR 0x0000), while the SAME client against
a fixed 4.0.7 binary returns the clean PROTOCOL_ERROR ``Invalid value for the paging state`` (0x000a) —
the A/B control proving the signature is the buggy paging-state parser.
"""

import base64
import logging

from sregym.conductor.oracles.cassandra_raw_ring_oracles import CassandraLogGrepOracle
from sregym.conductor.problems.cassandra_raw_ring import CassandraRawRingProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)

# Raw CQL native-protocol v4 client (pure stdlib). Sends a QUERY with a crafted paging_state
# (f0 7f ff ff ff) over a v4 connection. Buggy (<=4.0.6) -> SERVER_ERROR 0x0000 leaking
# "java.lang.IndexOutOfBoundsException: -2147483644"; fixed (>=4.0.7) -> PROTOCOL_ERROR 0x000a
# "Invalid value for the paging state". Staged into the pod (base64) and run via the in-image python3.
_CLIENT_SRC = r"""
import socket
import struct
import sys

HOST, PORT = "127.0.0.1", 9042
V4_REQ = 0x04


def frame(opcode, body, stream=1):
    return struct.pack(">BBhBi", V4_REQ, 0, stream, opcode, len(body)) + body


def cstring(s):
    b = s.encode("utf-8")
    return struct.pack(">h", len(b)) + b


def clongstring(s):
    b = s.encode("utf-8")
    return struct.pack(">i", len(b)) + b


def read_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise IOError("connection closed")
        buf += chunk
    return buf


def read_frame(sock):
    hdr = read_exact(sock, 9)
    _ver, _flags, _stream, opcode, length = struct.unpack(">BBhBi", hdr)
    body = read_exact(sock, length) if length else b""
    return opcode, body


def main():
    s = socket.create_connection((HOST, PORT), timeout=30)
    body = struct.pack(">h", 1) + cstring("CQL_VERSION") + cstring("3.0.0")
    s.sendall(frame(0x01, body))
    opcode, _ = read_frame(s)
    if opcode != 0x02:
        print("STARTUP did not yield READY, opcode=0x%02x" % opcode)
        sys.exit(2)

    query = "SELECT * FROM repro17840.t"
    paging_state = bytes([0xF0, 0x7F, 0xFF, 0xFF, 0xFF])
    consistency = 0x0001
    flags = 0x08
    qbody = (
        clongstring(query)
        + struct.pack(">h", consistency)
        + struct.pack(">B", flags)
        + struct.pack(">i", len(paging_state))
        + paging_state
    )
    s.sendall(frame(0x07, qbody))
    opcode, body = read_frame(s)
    pk_len = 0x7FFFFFFF
    index = 0 + 5 + pk_len
    print(
        "malicious paging_state hex=%s (pkLen=%d, vintsize=5, index=%d -> int wrap=%d)"
        % (paging_state.hex(), pk_len, index, struct.unpack(">i", struct.pack(">I", index & 0xFFFFFFFF))[0])
    )
    if opcode == 0x00:
        (code,) = struct.unpack(">i", body[:4])
        (mlen,) = struct.unpack(">h", body[4:6])
        message = body[6 : 6 + mlen].decode("utf-8", "replace")
        print("[MALICIOUS-overflow] reply opcode=0x00 len=%d" % len(body))
        print("[MALICIOUS-overflow] ERROR code=0x%08x message=%r" % (code & 0xFFFFFFFF, message))
    else:
        print("[MALICIOUS-overflow] unexpected reply opcode=0x%02x len=%d" % (opcode, len(body)))
    s.close()


if __name__ == "__main__":
    main()
"""


class AutoCassandra17840(CassandraRawRingProblem):
    """Single 4.0.5 node; a crafted v4 paging_state leaks IndexOutOfBoundsException (SERVER_ERROR)."""

    db_name = "cassandra"
    db_version = "4.0.5"
    cassandra_version = "4.0.5"
    source_git_ref = "cassandra-4.0.5"
    ring_namespace = "cassraw-17840"
    # Single node: the bug is coordinator-side paging-state parsing; RF=1 is sufficient.
    replicas = 1

    root_cause_file = "src/java/org/apache/cassandra/service/pager/PagingState.java"
    root_cause_description = (
        "IndexOutOfBoundsException in paging-state version inference. In "
        "PagingState.isModernSerialized, `index` is an `int` and the code does "
        "`index += computeUnsignedVIntSize(partitionKeyLen) + partitionKeyLen` (int += long) with no "
        "Math.toIntExact / overflow guard. A CQL native-protocol v4 QUERY carrying a crafted paging_state "
        "whose first unsigned-VInt (partitionKeyLen) decodes to a large positive value (0x7FFFFFFF) "
        "overflows `index` to a negative number (-2147483644), which still passes the `index >= limit` "
        "check; getUnsignedVInt is then called with that negative index and `input.get(-2147483644)` throws "
        "IndexOutOfBoundsException. Because that is not an IOException it escapes deserialize()'s "
        "`catch (IOException)` and leaks to the client as a SERVER_ERROR (error code 0x0000) instead of a "
        "clean PROTOCOL_ERROR. The fix (4.0.7) reads partitionKeyLen via toIntExact() (throws on overflow) "
        "and adds an `index < 0` guard (addNonNegative). Component: Messaging/Client."
    )

    _POD = "cass-0"
    _KS = "repro17840"
    _TABLE = "repro17840.t"
    _CLIENT = "/tmp/repro17840c.py"
    # Verbatim SERVER_ERROR message leaked by the buggy 4.0.5/4.0.6 coordinator. A fixed 4.0.7 binary
    # instead returns PROTOCOL_ERROR "Invalid value for the paging state", which does not match.
    _BUGGY_PATTERN = r"java\.lang\.IndexOutOfBoundsException: -2147483644"

    def _create_schema(self):
        self.app.cqlsh(
            self._POD,
            f"CREATE KEYSPACE IF NOT EXISTS {self._KS} WITH replication = "
            "{'class':'SimpleStrategy','replication_factor':1}; "
            f"CREATE TABLE IF NOT EXISTS {self._TABLE} (id int PRIMARY KEY, v text); "
            f"INSERT INTO {self._TABLE} (id, v) VALUES (1, 'a'); "
            f"INSERT INTO {self._TABLE} (id, v) VALUES (2, 'b'); "
            f"INSERT INTO {self._TABLE} (id, v) VALUES (3, 'c'); "
            f"INSERT INTO {self._TABLE} (id, v) VALUES (4, 'd'); "
            f"INSERT INTO {self._TABLE} (id, v) VALUES (5, 'e');",
        )

    def _stage_client(self):
        """Stage the raw native-protocol client into the node (base64, avoids any quoting hazard)."""
        b64 = base64.b64encode(_CLIENT_SRC.encode("utf-8")).decode("ascii")
        self.app.exec(self._POD, f"echo {b64} | base64 -d > {self._CLIENT}")

    def retrigger(self):
        """Idempotently (re)ensure the schema + staged client so the oracle's command can fire."""
        self._create_schema()
        self._stage_client()

    @mark_fault_injected
    def inject_fault(self):
        """Create the table, stage the raw v4 client, and fire the crafted-paging_state QUERY once."""
        self._create_schema()
        self._stage_client()
        out = self.app.exec(self._POD, f"python3 {self._CLIENT}")
        logger.info(f"[17840] inject_fault crafted-paging_state QUERY output:\n{out}")

    def build_mitigation_oracle(self):
        return CassandraLogGrepOracle(
            problem=self,
            pod=self._POD,
            source="command",
            command=f"python3 {self._CLIENT}",
            pattern=self._BUGGY_PATTERN,
            retrigger=True,
            attempts=3,
            retry_delay=10.0,
        )

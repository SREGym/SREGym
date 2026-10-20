"""Start nova-compute as this pod's hypervisor.

The StatefulSet pod name is the Nova host name. Each hypervisor reaches the
message broker with its own credential, derived from the region's host key, so
a single host can be revoked without rotating the shared control-plane secret.
"""

import hashlib
import hmac
import os
import socket


def broker_password(key: bytes, host: str) -> str:
    return hmac.new(key, host.encode(), hashlib.sha256).hexdigest()[:32]


def main() -> None:
    host = socket.gethostname()
    with open("/etc/broker/host-key", "rb") as handle:
        password = broker_password(handle.read().strip(), host)
    broker = os.environ["BROKER_HOST"]
    with open("/tmp/hypervisor.conf", "w") as handle:
        handle.write(
            "[DEFAULT]\n"
            f"host = {host}\n"
            f"my_ip = {os.environ.get('POD_IP', '127.0.0.1')}\n"
            f"transport_url = rabbit://{host}:{password}@{broker}:5672/\n"
        )
    os.execvp(
        "nova-compute",
        ["nova-compute", "--config-file", "/etc/nova/nova.conf", "--config-file", "/tmp/hypervisor.conf"],
    )


if __name__ == "__main__":
    main()

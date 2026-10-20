"""Create the region's initial Swift rings in the ring builder's directory (idempotent).

Every storage node is one zone; its devices are named d0..dN-1 and added node by
node, so device ids follow that order. Ring device addresses are the storage
pods' stable DNS names, which Swift resolves.
"""

import json
import os
import subprocess
import sys

BUILDERS = os.environ.get("BUILDER_DIR", "/etc/swift-builders")
PORTS = {"object": 6200, "container": 6201, "account": 6202}


def ring_builder(*args):
    return subprocess.run(["swift-ring-builder", *args], capture_output=True, text=True, check=False)


def main(nodes, devices, part_power, replicas=3, object_replicas=None, min_part_hours=1):
    os.makedirs(BUILDERS, exist_ok=True)
    created = []
    for ring, port in PORTS.items():
        builder = os.path.join(BUILDERS, f"{ring}.builder")
        if os.path.exists(builder):
            continue
        count = object_replicas if ring == "object" and object_replicas else replicas
        ring_builder(builder, "create", str(part_power), str(count), str(min_part_hours)).check_returncode()
        for zone, node in enumerate(nodes, start=1):
            for device in devices:
                # The compact r<region>z<zone>-<ip> form accepts only IP addresses.
                result = ring_builder(
                    builder,
                    "add",
                    "--region",
                    "1",
                    "--zone",
                    str(zone),
                    "--ip",
                    node,
                    "--port",
                    str(port),
                    "--device",
                    device,
                    "--weight",
                    "100",
                )
                result.check_returncode()
        result = ring_builder(builder, "rebalance")
        # Exit status 1 means "rebalanced with warnings", which is fine here.
        if result.returncode not in (0, 1):
            raise RuntimeError(result.stdout + result.stderr)
        created.append(ring)
    print(json.dumps({"created": created}))


if __name__ == "__main__":
    arguments = json.loads(sys.argv[1])
    main(**arguments)

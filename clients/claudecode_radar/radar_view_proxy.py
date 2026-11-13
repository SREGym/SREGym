"""Long-lived SREGym filtering proxy for a host-side Radar daemon.

Radar runs on the host, outside the agent container, so it does not pass through
the per-problem proxy the conductor starts for the agent's kubectl. This runs the
same class (sregym/service/k8s_proxy.py: same hidden namespaces and labels, Chaos
Mesh rules and filtered-mode write restrictions) as a persistent instance and
writes a kubeconfig that points Radar at it, so Radar sees the cluster view the
agent's kubectl sees.

Run from the repository root, with KUBECONFIG pointing at the benchmark cluster:

    uv run python -m clients.claudecode_radar.radar_view_proxy \\
        --kubeconfig-out /tmp/radar-view.kubeconfig
"""

import argparse
import logging
import time

from sregym.service.k8s_proxy import KubernetesAPIProxy


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=16450, help="Local port for the proxy (default: 16450).")
    parser.add_argument("--kubeconfig-out", required=True, help="Where to write Radar's kubeconfig.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    proxy = KubernetesAPIProxy(listen_port=args.port, listen_host="127.0.0.1", restrict_network_access=True)
    proxy.start()
    proxy.generate_agent_kubeconfig(args.kubeconfig_out)
    print(f"Radar view proxy on 127.0.0.1:{args.port}, kubeconfig {args.kubeconfig_out}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        proxy.stop()


if __name__ == "__main__":
    main()

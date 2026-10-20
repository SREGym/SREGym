"""Deploy SREGym's per-problem infra/observability stack the way Conductor.deploy_app does.

metrics-server, OpenEBS, Prometheus, Jaeger, the OTel collector and Loki (+promtail),
each timed. The MCP server is left out: it needs a locally built image.
"""

import logging
import time

from sregym.observer.jaeger import Jaeger
from sregym.observer.otel_collector import OtelCollector
from sregym.service.kubectl import KubeCtl
from sregym.service.telemetry.loki import Loki
from sregym.service.telemetry.prometheus import Prometheus

logging.basicConfig(level=logging.WARNING)

METRICS_SERVER = "https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml"
METRICS_SERVER_PATCH = (
    "kubectl -n kube-system patch deployment metrics-server --type=json -p='["
    '{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"},'
    '{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-preferred-address-types=InternalIP"}'
    "]'"
)
OPENEBS = "https://openebs.github.io/charts/openebs-operator.yaml"
OPENEBS_DEFAULT = (
    "kubectl patch storageclass openebs-hostpath "
    '-p \'{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}\''
)


def main():
    k = KubeCtl()
    times = {}
    t0 = time.time()

    t = time.time()
    k.exec_command(f"kubectl apply -f {METRICS_SERVER}")
    k.exec_command(METRICS_SERVER_PATCH)
    k.wait_for_ready("kube-system")
    times["metrics-server"] = time.time() - t

    t = time.time()
    k.exec_command(f"kubectl apply -f {OPENEBS}")
    k.exec_command(OPENEBS_DEFAULT)
    k.wait_for_ready("openebs")
    times["openebs"] = time.time() - t

    for name, component in [
        ("prometheus", Prometheus()),
        ("jaeger", Jaeger()),
        ("otel", OtelCollector()),
        ("loki", Loki()),
    ]:
        t = time.time()
        component.deploy()
        times[name] = time.time() - t

    print("OBS_TIMES", {k_: round(v, 1) for k_, v in times.items()}, "TOTAL", round(time.time() - t0, 1), flush=True)


if __name__ == "__main__":
    main()

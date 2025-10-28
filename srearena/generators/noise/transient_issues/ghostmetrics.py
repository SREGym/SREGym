import argparse
import json
import random
import time

from prometheus_client import CollectorRegistry, Gauge, push_to_gateway


def ghost_metrics(metric, amplitude, freq, duration, labels, pushgateway=None):
    reg = CollectorRegistry()
    g = Gauge(metric, "ghost metric", list(labels.keys()), registry=reg)
    gl = g.labels(**labels)
    start = time.time()
    while time.time() - start < duration:
        val = amplitude * (0.5 + random.random())
        gl.set(val)
        if pushgateway:
            push_to_gateway(pushgateway, job="ghost", registry=reg)
        else:
            print(f"[METRIC] {metric}={val} {labels}")
        time.sleep(freq)


def ghost_logs(rate, duration, labels):
    start = time.time()
    while time.time() - start < duration:
        msg = {
            "ghost": True,
            **labels,
            "ts": time.time(),
            "msg": random.choice(["sim req ok", "err timeout", "latency high"]),
        }
        print(json.dumps(msg))
        time.sleep(1.0 / rate)

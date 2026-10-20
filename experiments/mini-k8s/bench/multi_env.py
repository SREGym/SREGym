"""Deploy N copies of hotel-reservation with SREGym's own deploy code.

Each copy lives in namespace <prefix>-<i>. Prints per-copy deploy time and,
with --check, whether a probe through each copy's frontend Service answers.

usage: python multi_env.py N [--prefix hr] [--start 0] [--check] [--check-only]
"""

import argparse
import logging
import time

from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl

logging.basicConfig(level=logging.WARNING)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("n", type=int)
    ap.add_argument("--prefix", default="hr")
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--check-only", action="store_true", help="probe existing copies, deploy nothing")
    a = ap.parse_args()
    kubectl = KubeCtl()
    for i in range(a.start, a.start + a.n) if not a.check_only else []:
        app = HotelReservation()
        app.namespace = f"{a.prefix}-{i}"
        t0 = time.time()
        app.deploy()
        print(f"ENV {app.namespace} deployed in {time.time() - t0:.1f}s", flush=True)
    if a.check or a.check_only:
        for i in range(a.start, a.start + a.n):
            ns = f"{a.prefix}-{i}"
            out = kubectl.exec_command(
                f"kubectl run -n {ns} probe-{i} --rm -i --restart=Never --image=busybox:1.36 -- "
                "wget -qO- -T 5 'http://frontend:5000/hotels?inDate=2015-04-09&outDate=2015-04-10&lat=37.7749&lon=-122.4194'"
            )
            print(f"CHECK {ns}: {'ok' if 'features' in out or 'Feature' in out else 'FAIL ' + out[-200:]}", flush=True)


if __name__ == "__main__":
    main()

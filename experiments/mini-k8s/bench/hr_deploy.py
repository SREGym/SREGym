"""Deploy or clean up hotel-reservation with SREGym's own HotelReservation class, timed.

usage: python hr_deploy.py deploy|cleanup
"""

import logging
import sys
import time

from sregym.service.apps.hotel_reservation import HotelReservation

logging.basicConfig(level=logging.WARNING)


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "deploy"
    app = HotelReservation()
    t0 = time.time()
    if action == "deploy":
        app.deploy()
    elif action == "cleanup":
        app.cleanup()
    else:
        sys.exit(f"unknown action {action}")
    print(f"HR_{action.upper()}_SECONDS={time.time() - t0:.1f}", flush=True)


if __name__ == "__main__":
    main()

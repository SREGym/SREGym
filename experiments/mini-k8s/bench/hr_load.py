"""Start SREGym's default wrk2 workload (an in-cluster Job) against hotel-reservation."""

import logging

from sregym.service.apps.hotel_reservation import HotelReservation

logging.basicConfig(level=logging.WARNING)


def main():
    app = HotelReservation()
    app.create_workload(rate=100, connections=100, duration=30, threads=3)  # SREGym's defaults
    app.start_workload()
    print("WORKLOAD_STARTED", flush=True)


if __name__ == "__main__":
    main()

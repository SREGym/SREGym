"""Synthetic input and assertions executed inside the population image.

The ten application scripts are executed unchanged, in the chart job's order.
Keep default client negotiation: explicitly forcing RESP2 here would hide the
redis-py regression this test is intended to catch.
"""

import json
import os
import platform
import subprocess
import sys
from pathlib import Path

import pandas as pd
import redis

STEPS = ("distances", "station", "money", "orders", "trips", "planes", "priceroute", "sId", "sold_tickets", "entities")
EXPECTED_COUNTS = {
    "distances": 4,
    "station": 3,
    "stations": 4,
    "money": 600,
    "trips": 4,
    "rId": 4,
    "planeType": 4,
    "priceRoute": 4,
    "sId": 3,
    "soldTickets": 4,
    "entities": 4,
    "boughtDate": 200,
}


def main():
    client = redis.StrictRedis(host=os.environ["REDIS_HOST"], port=6379, db=1)
    assert client.ping()
    assert client.info()["redis_version"] == "4.0.14"
    rows = [
        {
            "flight_id": 800 + index,
            "route": origin + destination,
            "origin": origin,
            "destination": destination,
            "distance": distance,
            "ritinfare": 123.0,
            "price_productpassenger_weighted": 2.0,
            "passengers": 100,
            "numpassengers_product": 100,
            "year": 2016,
            "quarter": 1,
        }
        for index, (origin, destination, distance) in enumerate(
            [("SFO", "BOS", 2704), ("BOS", "SFO", 2704), ("SFO", "LAX", 337), ("LAX", "SFO", 337)]
        )
    ]
    data = Path("/app/clean/raw")
    data.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_stata(data / "DB1B_TICKETS_COUPONS_2016_1_fixture.dta", write_index=False)
    for step in STEPS:
        subprocess.run([sys.executable, f"/app/add_{step}.py"], cwd="/app", check=True, timeout=60)
    counts = {key: client.hlen(key) for key in EXPECTED_COUNTS}
    assert counts == EXPECTED_COUNTS, counts
    print(
        json.dumps(
            {
                "passed": True,
                "architecture": platform.machine(),
                "redis_py": redis.__version__,
                "redis_server": client.info()["redis_version"],
                "scripts": list(STEPS),
                "hash_counts": counts,
            }
        )
    )


if __name__ == "__main__":
    main()

"""Stdlib-only image load client. This module is run inside the workload pod."""

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPException
from urllib.parse import quote
from urllib.request import Request, urlopen


def _json(request, timeout):
    with urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise ValueError(f"unexpected HTTP {response.status} from {request.full_url}")
        return json.load(response)


def _fetch_image(frontend, picture, delay):
    request = Request(
        f"{frontend}/images/products/{quote(picture, safe='')}",
        headers={"x-envoy-fault-delay-request": str(delay), "Cache-Control": "no-cache"},
    )
    start = time.monotonic()
    with urlopen(request, timeout=30) as response:
        if response.status != 200 or not response.headers.get_content_type().startswith("image/"):
            raise ValueError(
                f"{picture}: expected an image, got HTTP {response.status} {response.headers.get_content_type()}"
            )
        size = 0
        while chunk := response.read(65536):
            size += len(chunk)
        if not size:
            raise ValueError(f"{picture}: empty image response")
    return round(time.monotonic() - start, 4)


def fetch_batch(frontend, flagd, flag, pool):
    started_at = time.time()
    flag_request = Request(
        f"{flagd}/ofrep/v1/evaluate/flags/{flag}",
        data=b'{"context":{}}',
        headers={"Content-Type": "application/json"},
    )
    flag_result = _json(flag_request, timeout=3)
    delay = flag_result.get("value") if isinstance(flag_result, dict) else None
    if type(delay) is not int or delay < 0:
        raise ValueError("flag evaluation did not provide a nonnegative integer delay")

    products = _json(Request(f"{frontend}/api/products"), timeout=5)
    if not isinstance(products, list) or not products:
        raise ValueError("the product catalog is empty or invalid")
    pictures = [product.get("picture") if isinstance(product, dict) else None for product in products]
    if not all(isinstance(picture, str) and picture for picture in pictures):
        raise ValueError("the product catalog has a missing image name")
    futures = [pool.submit(_fetch_image, frontend, picture, delay) for picture in dict.fromkeys(pictures)]
    durations = []
    errors = []
    for future in futures:
        try:
            durations.append(future.result())
        except (OSError, ValueError, HTTPException) as exc:
            errors.append(str(exc))
    if errors:
        raise ValueError(errors[0])
    return {"started_at": started_at, "flag_delay_ms": delay, "durations_s": durations}


def run(namespace, flag, interval):
    frontend = f"http://frontend-proxy.{namespace}.svc.cluster.local:8080"
    flagd = f"http://flagd.{namespace}.svc.cluster.local:8016"
    with ThreadPoolExecutor(max_workers=12) as pool:
        while True:
            started_at = time.time()
            try:
                sample = fetch_batch(frontend, flagd, flag, pool)
            except (OSError, ValueError, HTTPException) as exc:
                sample = {"started_at": started_at, "error": str(exc)}
            print(json.dumps(sample), flush=True)
            time.sleep(interval)


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2], int(sys.argv[3]))

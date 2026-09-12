"""Fixed-concurrency recommendation traffic against Astronomy Shop."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import requests

from sregym.generators.workload.hotel_search import KubectlPortForward


@dataclass(frozen=True)
class HerdSnapshot:
    submitted: int
    completed: int
    succeeded: int
    success_rate: float
    p95_latency_seconds: float | None
    p99_latency_seconds: float | None
    product_ids: tuple[str, ...]
    distinct_recommendation_sets: int


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(fraction * len(ordered)))
    return ordered[index]


def extract_product_ids(payload) -> list[str]:
    if isinstance(payload, dict):
        for key in ("productIds", "product_ids"):
            value = payload.get(key)
            if isinstance(value, list):
                return [str(item) for item in value if item]
        products = payload.get("products")
        if isinstance(products, list):
            ids = []
            for item in products:
                if isinstance(item, dict) and item.get("id"):
                    ids.append(str(item["id"]))
            if ids:
                return ids
    if isinstance(payload, list):
        ids = []
        for item in payload:
            if isinstance(item, dict) and item.get("id"):
                ids.append(str(item["id"]))
            elif isinstance(item, str) and item:
                ids.append(item)
        return ids
    return []


class RecommendationHerdWorkload:
    """Drive the public recommendation path at a fixed concurrency."""

    def __init__(
        self,
        namespace: str,
        frontend_service: str = "frontend-proxy",
        frontend_port: int = 8080,
        request_timeout: float = 8.0,
    ):
        self.namespace = namespace
        self.frontend = KubectlPortForward(namespace, frontend_service, frontend_port)
        self.request_timeout = request_timeout

    def start(self) -> None:
        self.frontend.start()

    def stop(self) -> None:
        self.frontend.stop()

    def _url(self, path: str) -> str:
        port = self.frontend.start()
        return f"http://127.0.0.1:{port}{path}"

    def catalog_product_ids(self) -> set[str]:
        response = requests.get(self._url("/api/products"), timeout=self.request_timeout)
        response.raise_for_status()
        return set(extract_product_ids(response.json()))

    def _one_request(self, product_ids: tuple[str, ...]) -> tuple[bool, float, tuple[str, ...]]:
        query = ",".join(product_ids)
        started = time.monotonic()
        try:
            response = requests.get(
                self._url(f"/api/recommendations?productIds={query}&_={time.time_ns()}"),
                headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
                timeout=self.request_timeout,
            )
            elapsed = time.monotonic() - started
            if response.status_code != 200:
                return False, elapsed, ()
            ids = tuple(extract_product_ids(response.json()))
            return bool(ids), elapsed, ids
        except (requests.RequestException, json.JSONDecodeError, ValueError):
            return False, time.monotonic() - started, ()

    def run(
        self,
        *,
        concurrency: int,
        duration_seconds: float,
        product_ids: tuple[str, ...],
    ) -> HerdSnapshot:
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        self.start()
        stop_at = time.monotonic() + duration_seconds
        submitted = 0
        completed = 0
        succeeded = 0
        latencies: list[float] = []
        returned_ids: list[str] = []
        recommendation_sets: set[tuple[str, ...]] = set()
        lock = threading.Lock()

        def worker() -> None:
            nonlocal submitted, completed, succeeded
            while time.monotonic() < stop_at:
                with lock:
                    submitted += 1
                ok, elapsed, ids = self._one_request(product_ids)
                with lock:
                    completed += 1
                    latencies.append(elapsed)
                    if ok:
                        succeeded += 1
                        returned_ids.extend(ids)
                        recommendation_sets.add(ids)

        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="rec-herd") as pool:
            futures = [pool.submit(worker) for _ in range(concurrency)]
            for future in as_completed(futures):
                future.result()

        success_rate = succeeded / completed if completed else 0.0
        unique_ids = tuple(dict.fromkeys(returned_ids))
        return HerdSnapshot(
            submitted=submitted,
            completed=completed,
            succeeded=succeeded,
            success_rate=success_rate,
            p95_latency_seconds=_percentile(latencies, 0.95),
            p99_latency_seconds=_percentile(latencies, 0.99),
            product_ids=unique_ids,
            distinct_recommendation_sets=len(recommendation_sets),
        )

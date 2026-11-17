"""A provider-managed transit hop that degrades when it is overrun.

Below its provisioned rate the hop is invisible. Once connections arrive faster
than that, the hop is impaired: every connection crossing it is slowed, and a
share of them lose packets and wait out retransmission, being reset if that
takes longer than a client would wait. The hop itself refuses nothing for being
one too many, so how much gets through depends on how many callers are waiting
on it.
An impaired hop stays impaired when load falls again; it recovers only when the
provider adds capacity.

A route marked ``/stream`` carries long-lived connections (a proxy tunnel), so
the hop slows each chunk sent across it rather than only the connection's
first: a kept-alive client feels the impairment on every request.

A route may also limit the connections each of the customer's instances holds
across it at once, as a provider bounds every instance's flows. A connection
beyond that waits for one of the instance's others to finish, and is reset if
none does in time. The provider knows its instances by listing them (pods
matching a selector) through the Kubernetes API.

Capacity is read from a local file that only the provider (the harness, through
``kubectl exec``) writes, and counters are published the same way. Neither is
reachable over the network.
"""

import asyncio
import contextlib
import json
import os
import random
import socket
import ssl
import struct
import time
import urllib.error
import urllib.parse
from collections import deque
from pathlib import Path
from urllib.request import Request, urlopen

STATE = Path(os.environ.get("TRANSIT_STATE", "/run/transit"))
#: ``listen=host:port`` pairs, comma separated; ``/stream`` slows every chunk.
ROUTES = os.environ.get("TRANSIT_ROUTES", "8065=mattermost.mattermost.svc:8065")
INITIAL_CAPACITY = float(os.environ.get("TRANSIT_CAPACITY", "50"))
#: Mean delay added to each connection while impaired, in seconds.
IMPAIRED_LATENCY = float(os.environ.get("TRANSIT_IMPAIRED_LATENCY", "0.8"))
#: Chance that each transmission attempt is lost while impaired.
IMPAIRED_LOSS = float(os.environ.get("TRANSIT_IMPAIRED_LOSS", "0.1"))
#: Overload has to last this long before the hop is impaired.
OVERLOAD_SECONDS = 2.0
#: First retransmission timeout; it doubles up to RTO_MAX.
RTO_INITIAL = 0.25
RTO_MAX = 4.0
#: A connection that has waited this long for retransmission is reset.
MAX_WAIT = float(os.environ.get("TRANSIT_MAX_WAIT", "12"))
STATS_SECONDS = 1.0
#: ``port=n`` pairs: connections one instance may hold across that route at once.
INSTANCE_FLOWS = os.environ.get("TRANSIT_INSTANCE_FLOWS", "")
INSTANCE_NAMESPACE = os.environ.get("TRANSIT_INSTANCE_NAMESPACE", "mattermost")
INSTANCE_SELECTOR = os.environ.get("TRANSIT_INSTANCE_SELECTOR", "")
#: A connection over its instance's limit is reset once it has waited this long.
INSTANCE_WAIT = float(os.environ.get("TRANSIT_INSTANCE_WAIT", "1"))
INSTANCE_SECONDS = 5.0
API = "https://kubernetes.default.svc"
ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")


def parse_routes(spec):
    routes = {}
    for item in filter(None, (part.strip() for part in spec.split(","))):
        listen, target = item.split("=", 1)
        target, _, mode = target.partition("/")
        host, port = target.rsplit(":", 1)
        routes[int(listen)] = (host, int(port), mode == "stream")
    return routes


def backoff(max_wait=MAX_WAIT, initial=RTO_INITIAL, cap=RTO_MAX):
    """Retransmission waits before a connection gives up, summing to max_wait."""
    waits, total, rto = [], 0.0, initial
    while total < max_wait:
        step = min(rto, max_wait - total)
        waits.append(step)
        total += step
        rto = min(cap, rto * 2)
    return waits


def parse_flows(spec):
    flows = {}
    for item in filter(None, (part.strip() for part in spec.split(","))):
        port, limit = item.split("=", 1)
        flows[int(port)] = int(limit)
    return flows


class RateMeter:
    """Connections that arrived in the last second."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.arrivals = deque()

    def hit(self):
        self.arrivals.append(self.clock())

    def rate(self):
        horizon = self.clock() - 1.0
        while self.arrivals and self.arrivals[0] <= horizon:
            self.arrivals.popleft()
        return len(self.arrivals)


class Link:
    def __init__(
        self,
        capacity,
        latency=IMPAIRED_LATENCY,
        loss=IMPAIRED_LOSS,
        draw=random.random,
        clock=time.monotonic,
        sustain=OVERLOAD_SECONDS,
    ):
        self.capacity = float(capacity)
        self.latency = latency
        self.loss = loss
        self.draw = draw
        self.clock = clock
        self.sustain = sustain
        self.impaired = False
        self.overloaded_since = None

    def observe(self, rate):
        if rate <= self.capacity:
            self.overloaded_since = None
            return
        now = self.clock()
        if self.overloaded_since is None:
            self.overloaded_since = now
        if now - self.overloaded_since >= self.sustain:
            self.impaired = True

    def set_capacity(self, capacity):
        capacity = float(capacity)
        if capacity > self.capacity:
            self.impaired = False
            self.overloaded_since = None
        self.capacity = capacity

    def waits(self):
        """Delays one connection sees, or None when it is reset."""
        if not self.impaired:
            return []
        waits = [self.latency * (0.75 + 0.5 * self.draw())]
        for step in backoff():
            if self.draw() >= self.loss:
                return waits
            waits.append(step)
        return None


class FlowLimit:
    """Connections each instance holds across one route; extra ones wait their turn."""

    def __init__(self, limit, max_wait=INSTANCE_WAIT):
        self.limit = limit
        self.max_wait = max_wait
        self.slots = {}

    async def acquire(self, source):
        slot = self.slots.get(source)
        if slot is None:
            slot = self.slots[source] = asyncio.Semaphore(self.limit)
        try:
            await asyncio.wait_for(slot.acquire(), self.max_wait)
        except TimeoutError:
            return False
        return True

    def release(self, source):
        self.slots[source].release()


link = Link(INITIAL_CAPACITY)
meter = RateMeter()
counters = {"accepted": 0, "forwarded": 0, "reset": 0, "waiting": 0, "target_errors": 0, "flow_limited": 0}
instances = set()


async def cross():
    meter.hit()
    link.observe(meter.rate())
    waits = link.waits()
    counters["waiting"] += 1
    try:
        for wait in waits or ():
            await asyncio.sleep(wait)
        if waits is None:
            await asyncio.sleep(MAX_WAIT)
            return False
        return True
    finally:
        counters["waiting"] -= 1


def reset(writer):
    sock = writer.get_extra_info("socket")
    if sock is not None:
        # SO_LINGER 0 closes with RST, as a dropped flow would end.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    writer.close()


async def pipe(reader, writer, delayed=False):
    try:
        while data := await reader.read(65536):
            if delayed:
                waits = link.waits()
                for wait in waits or ():
                    await asyncio.sleep(wait)
                if waits is None:
                    counters["reset"] += 1
                    reset(writer)
                    return
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()


def handler(host, port, stream, flows=None):
    async def handle(client_reader, client_writer):
        counters["accepted"] += 1
        source = (client_writer.get_extra_info("peername") or ("",))[0]
        limited = flows is not None and source in instances
        if limited and not await flows.acquire(source):
            counters["flow_limited"] += 1
            reset(client_writer)
            return
        try:
            await forward(client_reader, client_writer, host, port, stream)
        finally:
            if limited:
                flows.release(source)

    return handle


async def forward(client_reader, client_writer, host, port, stream):
    if not await cross():
        counters["reset"] += 1
        reset(client_writer)
        return
    try:
        upstream_reader, upstream_writer = await asyncio.open_connection(host, port)
    except OSError:
        counters["target_errors"] += 1
        reset(client_writer)
        return
    counters["forwarded"] += 1
    await asyncio.gather(pipe(client_reader, upstream_writer, stream), pipe(upstream_reader, client_writer))


def instance_addresses(namespace, selector):
    """Addresses of the customer's instances, or None when they cannot be listed."""
    try:
        token = (ACCOUNT / "token").read_text()
        context = ssl.create_default_context(cafile=str(ACCOUNT / "ca.crt"))
        query = urllib.parse.urlencode({"labelSelector": selector})
        request = Request(
            f"{API}/api/v1/namespaces/{namespace}/pods?{query}", headers={"Authorization": f"Bearer {token}"}
        )
        with urlopen(request, timeout=10, context=context) as response:
            items = json.loads(response.read())["items"]
    except (OSError, ValueError, KeyError, urllib.error.URLError):
        return None
    return {address for item in items if (address := item.get("status", {}).get("podIP"))}


async def track_instances():
    while True:
        found = await asyncio.to_thread(instance_addresses, INSTANCE_NAMESPACE, INSTANCE_SELECTOR)
        if found is not None:
            instances.clear()
            instances.update(found)
        await asyncio.sleep(INSTANCE_SECONDS)


def read_capacity(default):
    try:
        return float((STATE / "capacity").read_text().strip())
    except (OSError, ValueError):
        return default


async def publish():
    while True:
        capacity = read_capacity(link.capacity)
        if capacity != link.capacity:
            link.set_capacity(capacity)
        snapshot = {
            "at": time.time(),
            "capacity_per_second": link.capacity,
            "arrivals_per_second": meter.rate(),
            "impaired": link.impaired,
            **counters,
        }
        temporary = STATE / "stats.json.tmp"
        temporary.write_text(json.dumps(snapshot))
        temporary.replace(STATE / "stats.json")
        await asyncio.sleep(STATS_SECONDS)


async def main():
    STATE.mkdir(parents=True, exist_ok=True)
    if not (STATE / "capacity").exists():
        (STATE / "capacity").write_text(f"{INITIAL_CAPACITY}\n")
    flows = parse_flows(INSTANCE_FLOWS)
    servers = [
        await asyncio.start_server(
            handler(*target, flows=FlowLimit(flows[port]) if port in flows else None), "0.0.0.0", port, backlog=1024
        )
        for port, target in parse_routes(ROUTES).items()
    ]
    print(f"transit routes={ROUTES}", flush=True)
    tasks = [publish(), *(server.serve_forever() for server in servers)]
    if flows and INSTANCE_SELECTOR:
        tasks.append(track_instances())
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())

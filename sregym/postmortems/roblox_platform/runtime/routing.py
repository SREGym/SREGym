"""Tenant route tables backed by native Consul 1.10 streaming or blocking reads.

Subscriptions feed the tables used by application routing, rather than synthetic
profile values. The TCP bridge adds Consul's RPC protocol byte (8); all RPC work
and event fanout execute in the unmodified Consul server.
"""

import asyncio
import json
import os
import random
import time
from collections import Counter
from urllib.parse import parse_qs, urlsplit

import grpc
import requests
from subscription_pb2 import Event, SubscribeRequest

SERVICES = os.environ["ROUTE_SERVICES"].split(",")
TENANTS = int(os.environ.get("ROUTING_TENANTS", "8"))
MODE = os.environ.get("ROUTING_MODE", "stream")
CONSUL = os.environ["CONSUL_HTTP_ADDR"]
SERVERS = os.environ["CONSUL_SERVERS"].split(",")
TABLES = {}
COUNTERS = Counter()


async def bridge(reader, writer):
    remote = None
    try:
        # A connection reaches a real server, not an emulated subscription API.
        candidates = random.sample(SERVERS, len(SERVERS))
        for host in candidates:
            try:
                rr, remote = await asyncio.wait_for(asyncio.open_connection(host, 8300), 3)
                break
            except (TimeoutError, OSError):
                continue
        if remote is None:
            return
        remote.write(b"\x08")
        await remote.drain()

        async def copy(src, dst):
            while chunk := await src.read(65536):
                dst.write(chunk)
                await dst.drain()

        tasks = [asyncio.create_task(copy(reader, remote)), asyncio.create_task(copy(rr, writer))]
        _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        writer.close()
        if remote:
            remote.close()


def update(event, table):
    if event.NewSnapshotToFollow:
        table.clear()
    for child in event.EventBatch.Events:
        update(child, table)
    if event.HasField("ServiceHealth"):
        row = event.ServiceHealth.CheckServiceNode
        # Native deregistration events carry the node name but omit its IP.
        identity = row.Node.Node + ":" + row.Service.ID
        if event.ServiceHealth.Op == 1 or any(check.Status != "passing" for check in row.Checks):
            table.pop(identity, None)
        else:
            table[identity] = f"http://{row.Service.Address or row.Node.Address}:{row.Service.Port}"


async def watch(channel, tenant, service):
    rpc = channel.unary_stream(
        "/subscribe.StateChangeSubscription/Subscribe",
        request_serializer=SubscribeRequest.SerializeToString,
        response_deserializer=Event.FromString,
    )
    while True:
        table = {}
        TABLES[tenant, service] = table
        try:
            async for event in rpc(SubscribeRequest(Topic=1, Key=service)):
                update(event, table)
                COUNTERS["events"] += 1
        except grpc.RpcError as exc:
            COUNTERS["stream_errors"] += 1
            if COUNTERS["stream_errors"] % 100 == 1:
                print(json.dumps({"time": time.time(), "event": "subscription_error", "error": str(exc)}), flush=True)
            await asyncio.sleep(1)


async def poll(service):
    # Consul's blocking-query path needs one read per service and router. The
    # tenant route tables share that result because this platform's tenants use
    # the same service endpoints. Issuing one blocking HTTP request for every
    # tenant would exhaust the client's thread pool before the first snapshot.
    table = {}
    for tenant in range(TENANTS):
        TABLES[tenant, service] = table
    index = "0"
    while True:
        try:
            response = await asyncio.to_thread(
                requests.get, CONSUL + f"/v1/health/service/{service}?passing=true&index={index}&wait=30s", timeout=35
            )
            response.raise_for_status()
            index = response.headers.get("X-Consul-Index", "0")
            table.clear()
            table.update({
                row["Node"]["Node"] + ":" + row["Service"]["ID"]:
                f"http://{row['Service']['Address'] or row['Node']['Address']}:{row['Service']['Port']}"
                for row in response.json()
            })
            COUNTERS["poll_updates"] += 1
        except Exception:
            COUNTERS["poll_errors"] += 1
            await asyncio.sleep(1)


async def http(reader, writer):
    code = 200
    try:
        line = await asyncio.wait_for(reader.readline(), 3)
        target = urlsplit(line.decode().split()[1])
        if target.path == "/health":
            result = {"process": "running"}
        elif target.path == "/metrics":
            result = {**COUNTERS, "tables": len(TABLES), "populated": sum(bool(t) for t in TABLES.values())}
        else:
            args = parse_qs(target.query)
            key = int(args.get("tenant", [0])[0]) % TENANTS, args["service"][0]
            urls = list(TABLES.get(key, {}).values())
            if not urls:
                code = 503
            result = {"endpoints": urls}
        body = json.dumps(result).encode()
        writer.write(
            f"HTTP/1.1 {code} Result\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
    finally:
        writer.close()


async def main():
    bridge_server = await asyncio.start_server(bridge, "127.0.0.1", 0)
    port = bridge_server.sockets[0].getsockname()[1]
    channels = [
        grpc.aio.insecure_channel(f"127.0.0.1:{port}", options=(("grpc.use_local_subchannel_pool", 1),))
        for _ in range(16)
    ]
    tasks = []
    if MODE == "stream":
        for tenant in range(TENANTS):
            for service in SERVICES:
                tasks.append(asyncio.create_task(watch(channels[tenant % len(channels)], tenant, service)))
    elif MODE == "poll":
        for service in SERVICES:
            tasks.append(asyncio.create_task(poll(service)))
    else:
        raise ValueError(f"unknown routing mode: {MODE}")
    server = await asyncio.start_server(http, "0.0.0.0", int(os.environ["NOMAD_PORT_http"]))  # noqa: SIM112 -- native Nomad label
    print(json.dumps({"event": "routing_started", "mode": MODE, "tables": len(tasks)}), flush=True)
    async with server, bridge_server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())

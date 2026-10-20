#!/usr/bin/env python3
"""Forward LISTEN_HOST:LISTEN_PORT to 127.0.0.1:TARGET_PORT.

Sandbox helper: lets kind nodes (containers) reach an egress proxy that only
listens on the host's loopback.

usage: tcpfwd.py LISTEN_HOST LISTEN_PORT TARGET_PORT
"""

import asyncio
import sys


async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except OSError:
        pass
    finally:
        writer.close()


async def main(listen_host, listen_port, target_port):
    async def handle(client_r, client_w):
        try:
            up_r, up_w = await asyncio.open_connection("127.0.0.1", target_port)
        except OSError:
            client_w.close()
            return
        await asyncio.gather(pipe(client_r, up_w), pipe(up_r, client_w))

    server = await asyncio.start_server(handle, listen_host, listen_port)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], int(sys.argv[2]), int(sys.argv[3])))

"""Minimal HTTP forward proxy for the prototype's image pulls.

Run it on the host, then start the container with EGRESS_PROXY=<host>:<port>.
containerd on every node reaches it through a unix socket and uses CONNECT for
registries. Pods have no route to it. Standard library only.

Usage: python3 egress-proxy.py [--listen 0.0.0.0] [--port 3128]
"""

import argparse
import asyncio
from urllib.parse import urlsplit


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        request = await reader.readline()
        method, target, _ = request.decode("latin-1").split(" ", 2)
        headers = []
        while (line := await reader.readline()) not in (b"\r\n", b"\n", b""):
            headers.append(line)
        if method == "CONNECT":
            host, port = target.rsplit(":", 1)
            upstream_reader, upstream_writer = await asyncio.open_connection(host, int(port))
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
        else:
            # Plain HTTP with an absolute URL; one request per connection.
            url = urlsplit(target)
            upstream_reader, upstream_writer = await asyncio.open_connection(url.hostname, url.port or 80)
            path = (url.path or "/") + (f"?{url.query}" if url.query else "")
            kept = [h for h in headers if not h.lower().startswith((b"proxy-", b"connection:"))]
            upstream_writer.write(
                f"{method} {path} HTTP/1.1\r\n".encode() + b"".join(kept) + b"Connection: close\r\n\r\n"
            )
            await upstream_writer.drain()
    except (ValueError, OSError) as exc:
        writer.write(f"HTTP/1.1 502 Bad Gateway\r\n\r\n{exc}\r\n".encode())
        writer.close()
        return
    await asyncio.gather(_pipe(reader, upstream_writer), _pipe(upstream_reader, writer))


async def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--listen", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=3128)
    args = parser.parse_args()
    server = await asyncio.start_server(_handle, args.listen, args.port)
    print(f"egress proxy listening on {args.listen}:{args.port}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(_main())

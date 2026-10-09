"""Absolute deadlines for owner HTTP through qualified numeric forwards.

The pinned HTTPX/httpcore public transport and network interfaces keep response
headers, body reads and connection reuse under the same wall-clock budget.
"""

import ipaddress
import ssl
import time
from contextlib import contextmanager
from contextvars import ContextVar

import httpcore
import httpx

_budget = ContextVar("owner_http_budget", default=None)


def _remaining(timeout):
    deadline, cancelled = _budget.get()
    remaining = deadline - time.monotonic()
    if remaining <= 0 or (cancelled is not None and cancelled()):
        raise httpcore.ReadTimeout("Owner HTTP deadline or cancellation reached")
    return min(remaining, timeout if timeout is not None else remaining, 0.25)


@contextmanager
def _map_errors():
    try:
        yield
    except (httpcore.TimeoutException, httpcore.NetworkError, httpcore.ProtocolError) as error:
        for core, public in (
            (httpcore.ConnectTimeout, httpx.ConnectTimeout),
            (httpcore.ReadTimeout, httpx.ReadTimeout),
            (httpcore.WriteTimeout, httpx.WriteTimeout),
            (httpcore.PoolTimeout, httpx.PoolTimeout),
            (httpcore.ConnectError, httpx.ConnectError),
            (httpcore.ReadError, httpx.ReadError),
            (httpcore.WriteError, httpx.WriteError),
            (httpcore.RemoteProtocolError, httpx.RemoteProtocolError),
            (httpcore.LocalProtocolError, httpx.LocalProtocolError),
        ):
            if isinstance(error, core):
                raise public(str(error)) from error
        raise httpx.TransportError(str(error)) from error


class _Stream:
    def __init__(self, stream):
        self.stream = stream

    def read(self, max_bytes, timeout=None):
        while True:
            interval = _remaining(timeout)
            try:
                block = self.stream.read(max_bytes, timeout=interval)
                _remaining(timeout)
                return block
            except httpcore.ReadTimeout:
                _remaining(timeout)

    def write(self, buffer, timeout=None):
        connection = self.stream.get_extra_info("socket")
        while buffer:
            connection.settimeout(_remaining(timeout))
            try:
                sent = connection.send(buffer[:65536])
            except TimeoutError:
                continue
            except OSError as error:
                raise httpcore.WriteError(str(error)) from error
            if not sent:
                raise httpcore.WriteError("Owner HTTP connection closed during write")
            buffer = buffer[sent:]

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        deadline, _cancelled = _budget.get()
        _remaining(timeout)
        remaining = deadline - time.monotonic()
        upgraded = self.stream.start_tls(ssl_context, server_hostname, timeout=min(remaining, timeout or remaining))
        _remaining(timeout)
        return _Stream(upgraded)

    def get_extra_info(self, info):
        return self.stream.get_extra_info(info)

    def close(self):
        self.stream.close()


class _Backend:
    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        # Owner traffic uses captured loopback forwards. Avoid an unbounded
        # external DNS resolver while preserving TLS's original server name.
        address = "127.0.0.1" if host == "localhost" else host
        try:
            ipaddress.ip_address(address)
        except ValueError as error:
            raise httpcore.ConnectError("Owner HTTP requires a qualified numeric forwarding endpoint") from error
        deadline, _cancelled = _budget.get()
        _remaining(timeout)
        remaining = deadline - time.monotonic()
        stream = httpcore.SyncBackend().connect_tcp(
            address,
            port,
            timeout=min(remaining, timeout or remaining),
            local_address=local_address,
            socket_options=socket_options,
        )
        return _Stream(stream)


class _Body(httpx.SyncByteStream):
    def __init__(self, stream, budget):
        self.stream, self.budget = stream, budget

    def __iter__(self):
        token = _budget.set(self.budget)
        try:
            with _map_errors():
                yield from self.stream
        finally:
            _budget.reset(token)

    def close(self):
        self.stream.close()


class DeadlineTransport(httpx.BaseTransport):
    def __init__(self, *, verify=True, cancelled=None):
        if cancelled is not None and not callable(cancelled):
            raise ValueError("Deadline cancellation must be a callable observation")
        context = (
            verify
            if isinstance(verify, ssl.SSLContext)
            else ssl.create_default_context(cafile=verify if isinstance(verify, str) else None)
        )
        if verify is False:
            context.check_hostname, context.verify_mode = False, ssl.CERT_NONE
        self.cancelled = cancelled
        self.pool = httpcore.ConnectionPool(ssl_context=context, network_backend=_Backend(), max_connections=4)

    def handle_request(self, request):
        budget = (request.extensions.get("absolute_deadline", time.monotonic() + 10), self.cancelled)
        token = _budget.set(budget)
        try:
            core_request = httpcore.Request(
                method=request.method,
                url=httpcore.URL(
                    scheme=request.url.raw_scheme,
                    host=request.url.raw_host,
                    port=request.url.port,
                    target=request.url.raw_path,
                ),
                headers=request.headers.raw,
                content=request.stream,
                extensions=request.extensions,
            )
            with _map_errors():
                response = self.pool.handle_request(core_request)
            return httpx.Response(
                response.status,
                headers=response.headers,
                stream=_Body(response.stream, budget),
                extensions=response.extensions,
            )
        finally:
            _budget.reset(token)

    def close(self):
        self.pool.close()

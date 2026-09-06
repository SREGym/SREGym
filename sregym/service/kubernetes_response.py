"""JSON content negotiation and incremental Kubernetes HTTP responses."""

import json
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def json_accept_header(accept: str) -> str:
    """Preserve requested JSON Tables, but never negotiate opaque protobuf."""
    for item in accept.split(","):
        fields = [field.strip() for field in item.split(";")]
        parameters = dict(field.split("=", 1) for field in fields[1:] if "=" in field)
        if (
            fields[0] == "application/json"
            and parameters.get("as") == "Table"
            and parameters.get("g") == "meta.k8s.io"
            and parameters.get("v") in {"v1", "v1beta1"}
            and parameters.get("q", "1") != "0"
        ):
            return f"application/json;as=Table;g=meta.k8s.io;v={parameters['v']},application/json"
    return "application/json"


def include_table_objects(path: str) -> str:
    """Request full row objects so filtering can inspect labels and Secret type."""
    parsed = urlsplit(path)
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != "includeObject"]
    query.append(("includeObject", "Object"))
    return urlunsplit(parsed._replace(query=urlencode(query)))


def stream_response(handler, response, *, event_visible=None) -> None:
    """Forward log chunks or filter newline-delimited watch events as they arrive.

    The caller owns upstream cleanup. Once headers are sent, an error must
    close the stream rather than append a second HTTP response.
    """
    handler.close_connection = True
    handler.send_response(response.status)
    for name, value in response.getheaders():
        if name.lower() not in {"connection", "transfer-encoding", "content-length", "content-encoding"}:
            handler.send_header(name, value)
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.wfile.flush()
    if event_visible is None:
        while chunk := response.read1(64 * 1024):
            handler.wfile.write(chunk)
            handler.wfile.flush()
        return

    # Kubernetes watches send one JSON object per line. Bound malformed input
    # without limiting the lifetime of a healthy watch.
    limit = 16 * 1024 * 1024
    while line := response.readline(limit):
        if not line.endswith(b"\n"):
            raise ValueError("Incomplete or oversized Kubernetes watch event")
        event = json.loads(line)
        if not isinstance(event, dict) or not isinstance(event.get("object"), dict):
            raise ValueError("Invalid Kubernetes watch event")
        if event_visible(event):
            handler.wfile.write(line)
            handler.wfile.flush()

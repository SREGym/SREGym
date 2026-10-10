"""Executable configuration producer and edge; all control state survives restart.

The two ClickHouse identities represent two stages of a permissions rollout.
They query real system.columns; no injected outage flag controls the HTTP path.
"""

import argparse
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CONTROL = Path(os.environ.get("CONTROL_PATH", "/control"))
FEATURE_LIMIT = 200
PERIOD = 20
QUERY = "SELECT name, type FROM system.columns WHERE table = 'http_requests_features' ORDER BY name"
CANONICAL = [{"name": f"feature_{i:03}", "type": "Float32"} for i in range(120)]


def read(name):
    return json.loads((CONTROL / name).read_text())


def write(name, value):
    target = CONTROL / name
    temporary = target.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(target)


def query(sql, user="operator"):
    url = "http://feature-catalog:8123/"
    headers = {"X-ClickHouse-User": user}
    if user == "operator":
        headers["X-ClickHouse-Key"] = os.environ["CATALOG_PASSWORD"]
    request = urllib.request.Request(url, data=sql.encode(), headers=headers)
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read().decode()


def features(user):
    sql = (CONTROL / "query.sql").read_text().strip().rstrip(";")
    return json.loads(query(sql + " FORMAT JSON", user))["data"]


def bootstrap():
    CONTROL.mkdir(parents=True, exist_ok=True)
    if not (CONTROL / "initialized").exists():
        columns = ", ".join(f"{f['name']} Float32" for f in CANONICAL)
        for sql in (
            "CREATE DATABASE IF NOT EXISTS r0",
            f"CREATE TABLE IF NOT EXISTS default.http_requests_features ({columns}) ENGINE=Memory",
            f"CREATE TABLE IF NOT EXISTS r0.http_requests_features ({columns}) ENGINE=Memory",
            "CREATE USER IF NOT EXISTS feature_old IDENTIFIED WITH no_password",
            "CREATE USER IF NOT EXISTS feature_new IDENTIFIED WITH no_password",
            "GRANT SELECT ON default.http_requests_features TO feature_old, feature_new",
        ):
            query(sql)
        (CONTROL / "query.sql").write_text(QUERY + "\n")
        write(
            "settings.json",
            {"enabled": True, "bot_management_enabled": True, "sources": ["feature_old", "feature_new"]},
        )
        write("known-good.json", {"features": CANONICAL, "source": "validated-release", "generated_at": time.time()})
        write("current.json", read("known-good.json"))
        (CONTROL / "initialized").touch()


def generate(user):
    value = {"features": features(user), "source": user, "generated_at": time.time()}
    write("current.json", value)
    event = {"at": value["generated_at"], "source": user, "feature_count": len(value["features"])}
    with (CONTROL / "generation.jsonl").open("a") as stream:
        stream.write(json.dumps(event) + "\n")
    print(json.dumps(event), flush=True)
    return value


def producer():
    while True:
        try:
            bootstrap()
            break
        except Exception as exc:
            print(f"catalog startup: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(3)
    index = 0
    while True:
        try:
            settings = read("settings.json")
            if settings["enabled"]:
                sources = settings["sources"]
                generate(sources[index % len(sources)])
                index += 1
        except Exception as exc:
            print(f"generation failed: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(PERIOD)


def safety():
    """Inspect the deployed query under every configured rollout identity."""
    settings, current = read("settings.json"), read("current.json")
    result = {"settings": settings, "current_features": len(current["features"]), "source_counts": {}}
    if not settings["bot_management_enabled"]:
        return {**result, "safe": True, "containment": "approved-bot-kill-switch"}
    valid = sorted(current["features"], key=lambda f: f["name"]) == CANONICAL
    if settings["enabled"]:
        if not settings["sources"]:
            valid = False
        for source in settings["sources"]:
            rows = features(source)
            result["source_counts"][source] = len(rows)
            valid = valid and sorted(rows, key=lambda f: f["name"]) == CANONICAL
    return {**result, "safe": valid}


class Edge(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(fmt % args, flush=True)

    def respond(self, code, body, headers=None):
        self.send_response(code)
        for key, value in (headers or {}).items():
            if key.lower() not in ("connection", "transfer-encoding", "content-length", "server", "date"):
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.proxy()

    def do_POST(self):
        self.proxy()

    def do_DELETE(self):
        self.proxy()

    def proxy(self):
        # Kubernetes process readiness is deliberately weaker than business health.
        if self.path == "/ready":
            return self.respond(200 if (CONTROL / "current.json").exists() else 503, b"edge process ready")
        try:
            settings, config = read("settings.json"), read("current.json")
            count = len(config["features"])
            if self.path == "/metrics":
                return self.respond(200, f"edge_features {count}\nedge_feature_limit {FEATURE_LIMIT}\n".encode())
            if settings["bot_management_enabled"] and count > FEATURE_LIMIT:
                print(
                    f"BotConfig panic: {count} features exceeds fixed capacity {FEATURE_LIMIT}; request={self.path}",
                    flush=True,
                )
                return self.respond(500, b'{"error":"BotConfig feature vector capacity exceeded"}')
            upstream = "http://stripe-receiver:8080" if self.path.startswith("/hook") else "http://stripe-origin:8000"
            body = self.rfile.read(int(self.headers.get("Content-Length", 0))) if self.command == "POST" else None
            headers = {
                k: v for k, v in self.headers.items() if k.lower() not in ("host", "connection", "content-length")
            }
            request = urllib.request.Request(upstream + self.path, data=body, headers=headers, method=self.command)
            try:
                with urllib.request.urlopen(request, timeout=15) as response:
                    self.respond(response.status, response.read(), dict(response.headers))
            except urllib.error.HTTPError as exc:
                self.respond(exc.code, exc.read(), dict(exc.headers))
        except Exception as exc:
            print(f"edge error: {type(exc).__name__}: {exc}", flush=True)
            self.respond(503, b'{"error":"edge dependency unavailable"}')


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("edge", "producer", "safety", "generate", "sql", "settings", "known-good"))
    parser.add_argument("argument", nargs="?")
    args = parser.parse_args()
    if args.action == "edge":
        ThreadingHTTPServer(("0.0.0.0", 8000), Edge).serve_forever()
    elif args.action == "producer":
        producer()
    elif args.action == "safety":
        print(json.dumps(safety()))
    elif args.action == "generate":
        generate(args.argument)
    elif args.action == "sql":
        print(query(args.argument))
    elif args.action == "settings":
        write("settings.json", {**read("settings.json"), **json.loads(args.argument)})
    else:
        write("current.json", read("known-good.json"))

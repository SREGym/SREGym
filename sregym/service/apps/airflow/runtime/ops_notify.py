"""ops-notify: relays pipeline notices to the on-call channels.

POST /notify records a notice (JSON with at least ``channel`` and ``text``) in
the shop database table ``ops_notifications``; GET /notifications lists the
latest ones.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sqlalchemy import create_engine, text

engine = create_engine(os.environ["SHOP_DB_URL"], pool_pre_ping=True, pool_size=2, max_overflow=2)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        body = json.dumps(payload, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/notify":
            return self._send(404, {"error": "not found"})
        try:
            notice = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            with engine.begin() as conn:
                notice_id = conn.execute(
                    text(
                        "INSERT INTO ops_notifications (channel, dag_id, run_id, payload) "
                        "VALUES (:channel, :dag_id, :run_id, CAST(:payload AS jsonb)) RETURNING id"
                    ),
                    {
                        "channel": notice.get("channel") or "default",
                        "dag_id": notice.get("dag_id"),
                        "run_id": notice.get("run_id"),
                        "payload": json.dumps(notice),
                    },
                ).scalar()
        except Exception as error:
            print(f"WARNING ops-notify: could not record notice: {error}", flush=True)
            return self._send(503, {"error": str(error)})
        print(f"INFO ops-notify: [{notice.get('channel')}] {notice.get('text')}", flush=True)
        return self._send(201, {"id": notice_id})

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, {"ok": True})
        if self.path.startswith("/notifications"):
            with engine.connect() as conn:
                rows = (
                    conn.execute(
                        text(
                            "SELECT id, received_at, channel, dag_id, run_id, payload->>'text' AS text "
                            "FROM ops_notifications ORDER BY id DESC LIMIT 50"
                        )
                    )
                    .mappings()
                    .all()
                )
            return self._send(200, [dict(r) for r in rows])
        return self._send(404, {"error": "not found"})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()

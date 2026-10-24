"""Gitea webhook receiver."""

import hashlib
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SECRET = os.environ["WEBHOOK_SECRET"].encode()
deliveries = []


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        expected = hmac.new(SECRET, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, self.headers.get("X-Gitea-Signature", "")):
            self.send_response(403)
            self.end_headers()
            return
        payload = json.loads(body)
        issue = payload.get("issue") or {}
        comment = payload.get("comment") or {}
        deliveries.append(
            {
                "delivery": self.headers.get("X-Gitea-Delivery"),
                "event": self.headers.get("X-Gitea-Event"),
                "action": payload.get("action"),
                "repository": (payload.get("repository") or {}).get("full_name"),
                "issue_id": issue.get("id"),
                "number": issue.get("number"),
                "title": issue.get("title"),
                "body": issue.get("body"),
                "state": issue.get("state"),
                "comment_id": comment.get("id"),
                "comment": comment.get("body"),
            }
        )
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        data = json.dumps(deliveries).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


ThreadingHTTPServer(("", 8080), Handler).serve_forever()

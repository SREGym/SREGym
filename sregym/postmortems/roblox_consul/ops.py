#!/usr/bin/env python3
"""Agent-side client. No runner credentials or grading implementation required."""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

parser = argparse.ArgumentParser(description="Incident operations: ops help; ops ACTION '{JSON arguments}'")
parser.add_argument("action", nargs="?", default="help")
parser.add_argument("args", nargs="?", default="{}")
options = parser.parse_args()
try:
    payload = json.dumps({"action": options.action, "args": json.loads(options.args)}).encode()
    req = urllib.request.Request(
        os.environ.get("OPS_URL", "http://control:8080") + "/ops",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        print(json.dumps(json.load(response), indent=2))
except urllib.error.HTTPError as exc:
    print(exc.read().decode(), file=sys.stderr)
    sys.exit(1)

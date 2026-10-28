#!/bin/bash
# Reference solution: run this problem's recover_fault() in the SREGym backend.
# The backend accepts this only from Harbor's oracle agent, which copies this
# script into the container, where the agent user cannot write, before running it.
set -euo pipefail
curl -fsS --max-time 1800 -X POST "http://127.0.0.1:{{api_port}}/oracle/recover"
echo

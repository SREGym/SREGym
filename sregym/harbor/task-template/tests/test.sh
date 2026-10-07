#!/bin/bash
# Runs as root. Stop everything the agent left running, then grade once.
pkill -KILL -u agent 2>/dev/null || true
curl -fsS --max-time {{grade_timeout}} -X POST \
    -H "Authorization: Bearer $(cat {{grade_token_path}})" \
    "http://127.0.0.1:{{grade_port}}/grade" -o /dev/null \
    || echo "The SREGym backend did not answer the grade request." >&2
exec python3 /tests/score.py

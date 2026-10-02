#!/bin/bash
# Reference solution: run this problem's recover_fault() in the SREGym sidecar.
# The token is derived at run time from {{oracle_secret_env}}, which Harbor
# passes to the oracle agent only (task.toml [solution] env), so the task
# itself carries no usable token.
set -euo pipefail
token=$(python3 -c 'import hashlib, hmac, os, sys
print(hmac.new(os.environ[sys.argv[1]].encode(), sys.argv[2].encode(), hashlib.sha256).hexdigest())' \
    {{oracle_secret_env}} {{task_name}})
curl -fsS --max-time 1800 -X POST \
    -H "Authorization: Bearer $token" \
    "http://{{service_name}}:{{api_port}}/oracle/recover"
echo

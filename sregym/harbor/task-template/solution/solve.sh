#!/bin/bash
# Reference solution: run this problem's recover_fault() in the SREGym sidecar.
set -euo pipefail
curl -fsS --max-time 1800 -X POST \
    -H "Authorization: Bearer {{oracle_token}}" \
    "http://{{service_name}}:{{api_port}}/oracle/recover"
echo

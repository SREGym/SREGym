#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-social-network}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
kubectl -n "$NAMESPACE" get cm nginx-thrift -o json > "$tmp/cm.json"
python3 - "$tmp/cm.json" "$tmp/patch.json" <<'PYCODE'
import json,sys
obj=json.load(open(sys.argv[1]))
conf=obj['data']['nginx.conf']
block='    do\n      local TSocket = require "TSocket"\n      local TTransportException = require("TTransport").TTransportException\n      local terror = require("Thrift").terror\n      function TSocket:open()\n        if not self.handle then\n          self.handle = ngx.socket.tcp()\n          self.handle:settimeout(self.timeout)\n        end\n        local pool_key = tostring(self.port)\n        local ok, err = self.handle:connect(self.host, self.port, {pool = pool_key})\n        if not ok then\n          terror(TTransportException:new{\n            message = "Could not connect to " .. self.host .. ":" .. self.port .. " (" .. err .. ")"\n          })\n        end\n      end\n    end\n'
fixed=block.replace('local pool_key = tostring(self.port)', 'local pool_key = self.host .. ":" .. tostring(self.port)')
base=conf.replace(block,'').replace(fixed,'')
conf=base
json.dump({'data':{'nginx.conf':conf}},open(sys.argv[2],'w'))
PYCODE
kubectl -n "$NAMESPACE" patch cm nginx-thrift --type merge --patch-file "$tmp/patch.json"
# Restart workers to apply startup configuration and discard old pooled connections.
kubectl -n "$NAMESPACE" patch deployment nginx-thrift --type merge -p '{"spec":{"template":{"metadata":{"annotations":{"kubectl.kubernetes.io/restartedAt":null}}}}}'
kubectl -n "$NAMESPACE" rollout status deployment/nginx-thrift --timeout=180s

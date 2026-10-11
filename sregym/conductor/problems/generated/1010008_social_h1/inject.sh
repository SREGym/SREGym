#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-social-network}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
kubectl -n "$NAMESPACE" get cm nginx-thrift -o json > "$tmp/cm"
python3 - "$tmp" <<'PYCODE'
import json,sys,re
p=sys.argv[1]
c=json.load(open(p+'/cm'))
module='''local RpcClient = require "RpcClient"
local RpcCodecCache = require "RpcCodecCache"
local RpcClientFactory = RpcClient:new({ __type = 'Client' })
local codecs = RpcCodecCache:new()
function RpcClientFactory:createClient(thriftClient, ip, port, timeout, ssl)
    local key = ip .. ':' .. tostring(port)
    local protocol = codecs:get(key)
    if protocol then
        protocol.trans.rBuf = ''
        protocol.trans.wBuf = ''
        protocol.trans:open()
    else
        protocol = self:init(ip, port, timeout, ssl)
        codecs:put(key, protocol)
    end
    return thriftClient:new{iprot = protocol, oprot = protocol}
end
return RpcClientFactory
'''
cache='''-- Copies codec object graphs while preserving their class metatables.
local RpcCodecCache = {}
RpcCodecCache.__index = RpcCodecCache
local function copy(value, seen)
    if type(value) ~= 'table' then return value end
    if seen[value] then return seen[value] end
    local result = {}
    seen[value] = result
    for key, item in pairs(value) do
        result[copy(key, seen)] = copy(item, seen)
    end
    return setmetatable(result, getmetatable(value))
end
function RpcCodecCache:new()
    return setmetatable({entries = {}}, self)
end
function RpcCodecCache:get(key)
    local entry = self.entries[key]
    if entry then return copy(entry, {}) end
end
function RpcCodecCache:put(key, codec)
    self.entries[key] = codec
end
return RpcCodecCache
'''
conf=c['data']['nginx.conf']
conf=re.sub(r'    -- RPC codec setup\n.*?    -- RPC codec setup complete\n','',conf,flags=re.S)
block='    -- RPC codec setup\n'
for name,source in [('RpcCodecCache',cache),('RpcClientFactory',module)]:
 block+='    package.preload["'+name+'"] = function()\n'
 block+=''.join('      '+line+'\n' for line in source.splitlines())
 block+='    end\n'
block+='    -- RPC codec setup complete\n'
anchor='    local bridge_tracer = require "opentracing_bridge_tracer"'
assert anchor in conf
conf=conf.replace(anchor,block+anchor,1)
json.dump({'data':{'nginx.conf':conf}},open(p+'/cm-patch','w'))
PYCODE
kubectl -n "$NAMESPACE" patch cm nginx-thrift --type merge --patch-file "$tmp/cm-patch"
kubectl -n "$NAMESPACE" rollout restart deploy/nginx-thrift
kubectl -n "$NAMESPACE" rollout status deploy/nginx-thrift --timeout=180s

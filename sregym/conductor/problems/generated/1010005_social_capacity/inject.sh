#!/bin/bash
set -euo pipefail
export NAMESPACE="${NAMESPACE:-social-network}"
python3 - <<'PY'
import json, os, subprocess
ns=os.environ['NAMESPACE']
def k(*args): return subprocess.check_output(['kubectl','-n',ns,*args],text=True)
source=r'''local Object = require 'Object'
local RpcClientFactory = require 'RpcClientFactory'
local redis = require 'resty.redis'
local ngx = ngx
local GenericObjectPool = Object:new({
    __type = 'GenericObjectPool',
    maxTotal = 100,
    maxIdleTime = 10000,
    timeout = 10000,
    admissionCapacity = 64,
    leaseSeconds = 3600
})
local acquire = [[
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[2]) then return 0 end
redis.call('ZADD', KEYS[1], ARGV[3], ARGV[4])
redis.call('EXPIRE', KEYS[1], ARGV[5])
return 1
]]
local function registry()
    local r = redis:new()
    r:set_timeout(1000)
    local suffix = os.getenv('fqdn_suffix') or ''
    local ok, err = r:connect('social-graph-redis' .. suffix, 6379)
    if not ok then error('RPC admission registry unavailable: ' .. tostring(err)) end
    return r
end
function GenericObjectPool:init(conf) end
function GenericObjectPool:connection(thriftClient, ip, port)
    local key = 'rpc:admission:' .. ip .. ':' .. port
    local token = ngx.var.request_id .. ':' .. tostring(ngx.now())
    local r = registry()
    local now = ngx.now()
    local admitted, err = r:eval(acquire, 1, key, now-self.leaseSeconds,
        self.admissionCapacity, now, token, self.leaseSeconds*2)
    r:set_keepalive(10000, 100)
    if admitted ~= 1 then
        error('RPC request queue unavailable for ' .. ip .. ':' .. port .. ': ' .. tostring(err or 'concurrency budget exhausted'))
    end
    local ssl = ngx.shared.config:get('ssl')
    local client = RpcClientFactory:createClient(thriftClient, ip, port, self.timeout, ssl)
    client.admissionKey = key
    client.admissionToken = token
    return client
end
function GenericObjectPool:returnConnection(client)
    if client ~= nil then
        local transport = client.iprot.trans.trans
        if transport:isOpen() then
            local returned = transport:setKeepAlive(self.maxIdleTime, self.maxTotal)
            if returned then
                local r = registry()
                r:zrem(client.admissionKey, client.admissionToken)
                r:set_keepalive(10000, 100)
            end
        else
            ngx.log(ngx.ERR, 'return rpc client fail, socket close.')
        end
    end
end
function GenericObjectPool:setMaxTotal(value) self.maxTotal = value end
function GenericObjectPool:setmaxIdleTime(value) self.maxIdleTime = value end
function GenericObjectPool:setTimeout(value) self.timeout = value end
function GenericObjectPool:clear() end
function GenericObjectPool:remove() end
return GenericObjectPool
'''
cm=json.loads(k('get','cm','nginx-thrift','-o','json'))
start='    -- RPC admission control\n'
end='    -- End RPC admission control\n'
conf=cm['data']['nginx.conf']
import re
conf=re.sub(re.escape(start)+r'.*?'+re.escape(end), '', conf, flags=re.S)
block=start + "    package.loaded['GenericObjectPool'] = (function()\n" + source + "    end)()\n" + end
anchor='    local GenericObjectPool = require "GenericObjectPool"\n'
assert conf.count(anchor)==1
conf=conf.replace(anchor,anchor+block)
if cm['data']['nginx.conf'] != conf:
 k('patch','cm','nginx-thrift','--type=merge','-p',json.dumps({'data':{'nginx.conf':conf}}))
 k('rollout','restart','deploy/nginx-thrift')

PY
kubectl -n "$NAMESPACE" rollout status deployment/nginx-thrift --timeout=180s

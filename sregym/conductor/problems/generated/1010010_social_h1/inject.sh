#!/bin/bash
set -euo pipefail
export NAMESPACE="${NAMESPACE:-social-network}"
python3 - <<'PY'
import os,json,subprocess,re
ns=os.environ['NAMESPACE']
def k(*a): return subprocess.check_output(['kubectl','-n',ns,*a],text=True)
cm=json.loads(k('get','cm','nginx-thrift','-o','json'))
conf=cm['data']['nginx.conf']
conf=re.sub(r'    -- RPC completion adapter\n.*?    -- End RPC completion adapter\n','',conf,flags=re.S)
conf=re.sub(r'    (?:--|#) Transport session initialization\n.*?    (?:--|#) End Transport session initialization\n\n','',conf,flags=re.S)
completion=r'''    -- RPC completion adapter
    do
      local redis = require 'resty.redis'
      local pool = require 'GenericObjectPool'
      local returnConnection = pool.returnConnection
      function pool:returnConnection(client)
        local session = client and client.iprot.trans.trans
        returnConnection(self, client)
        if session and session.reservation then
          local r = redis:new()
          r:set_timeout(1000)
          local ok = r:connect('social-graph-redis' .. (os.getenv('fqdn_suffix') or ''), 6379)
          if ok then
            r:zrem(session.reservation.key, session.reservation.id)
            r:hdel(session.reservation.journal, session.reservation.id)
            r:set_keepalive(10000, 100)
          end
        end
      end
    end
    -- End RPC completion adapter
'''
transport=r'''    # Transport session initialization
  init_worker_by_lua_block {
    local socket = require 'TSocket'
    local redis = require 'resty.redis'
    local transport = require 'TTransport'
    local terror = require('Thrift').terror
    local open = socket.open
    local acquire = [[
      local now = redis.call('TIME')
      local stamp = tonumber(now[1]) + tonumber(now[2])/1000000
      local expired = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', stamp-tonumber(ARGV[2]))
      for _, member in ipairs(expired) do redis.call('HDEL', KEYS[2], member) end
      redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', stamp-tonumber(ARGV[2]))
      local receipts = redis.call('HGETALL', KEYS[2])
      for i=1,#receipts,2 do
        if tonumber(receipts[i+1]) > stamp-tonumber(ARGV[2]) then
          redis.call('ZADD', KEYS[1], receipts[i+1], receipts[i])
        else
          redis.call('HDEL', KEYS[2], receipts[i])
        end
      end
      if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[3]) then return 0 end
      local member = redis.sha1hex(ARGV[1])
      redis.call('ZADD', KEYS[1], stamp, member)
      redis.call('HSET', KEYS[2], member, stamp)
      redis.call('EXPIRE', KEYS[2], tonumber(ARGV[2])*2)
      redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2])*2)
      return 1
    ]]
    function socket:open()
      local id = ngx.var.request_id .. ':' .. tostring(ngx.now()) .. ':' .. tostring(ngx.worker.pid())
      local key = 'rpc:sessions:' .. self.host .. ':' .. tostring(self.port)
      local journal = 'rpc:session-receipts:' .. self.host .. ':' .. tostring(self.port)
      local r = redis:new()
      r:set_timeout(1000)
      local ok = r:connect('social-graph-redis' .. (os.getenv('fqdn_suffix') or ''), 6379)
      local admitted
      if ok then
        admitted = r:eval(acquire, 2, key, journal, id, 7200, 64)
        r:set_keepalive(10000, 100)
      end
      if admitted ~= 1 then
        ngx.sleep(0.05)
        terror(transport.TTransportException:new{
          message = 'Could not connect to ' .. self.host .. ':' .. tostring(self.port) .. ' (timeout)'
        })
      end
      self.reservation = {key = key, journal = journal, id = id}
      return open(self)
    end
  }
    # End Transport session initialization
'''
anchor='    local GenericObjectPool = require "GenericObjectPool"\n'
assert conf.count(anchor)==1
conf=conf.replace(anchor,anchor+completion)
anchor2='  server {\n'
assert conf.count(anchor2)==1
conf=conf.replace(anchor2,transport+'\n'+anchor2)
# Keep insertion idempotent, including the blank line outside the worker block.
conf=conf.replace('    # End Transport session initialization\n\n\n','    # End Transport session initialization\n\n')
if conf != cm['data']['nginx.conf']:
 k('patch','cm','nginx-thrift','--type=merge','-p',json.dumps({'data':{'nginx.conf':conf}}))
 pods=json.loads(k('get','pods','-l','service=nginx-thrift','-o','json'))['items']
 for p in pods: k('delete','pod',p['metadata']['name'],'--wait=true')
PY
kubectl -n "$NAMESPACE" rollout status deployment/nginx-thrift --timeout=180s
kubectl -n "$NAMESPACE" wait --for=condition=Ready pod -l service=nginx-thrift --timeout=180s

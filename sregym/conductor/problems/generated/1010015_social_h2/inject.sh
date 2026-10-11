#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-social-network}
ARTIFACT_DIR=${ARTIFACT_DIR:-$(cd "$(dirname "$0")" && pwd)}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
kubectl -n "$NAMESPACE" get cm nginx-thrift -o json > "$tmp/cm.json"
kubectl -n "$NAMESPACE" get deploy nginx-thrift -o json > "$tmp/dep.json"
if python3 - "$tmp/cm.json" "$ARTIFACT_DIR/original-cm.json" <<'PYUID'
import json,sys,os
current,saved=sys.argv[1:]
sys.exit(0 if not os.path.exists(saved) or json.load(open(current))['metadata']['uid'] != json.load(open(saved))['metadata']['uid'] else 1)
PYUID
then
 cp "$tmp/cm.json" "$ARTIFACT_DIR/original-cm.json"
 cp "$tmp/dep.json" "$ARTIFACT_DIR/original-dep.json"
fi
python3 - inject "$tmp" <<'PYCODE'
import json,sys
mode,path=sys.argv[1:]
c=json.load(open(path+'/cm.json')); d=json.load(open(path+'/dep.json'))
if mode=='inject':
 c['data']['nginx.conf']=__import__('re').sub(r'worker_processes\s+(?:auto|1);', 'worker_processes  1;', c['data']['nginx.conf'])
 source='--\n-- Licensed to the Apache Software Foundation (ASF) under one\n-- or more contributor license agreements. See the NOTICE file\n-- distributed with this work for additional information\n-- regarding copyright ownership. The ASF licenses this file\n-- to you under the Apache License, Version 2.0 (the\n-- "License"); you may not use this file except in compliance\n-- with the License. You may obtain a copy of the License at\n--\n--   http://www.apache.org/licenses/LICENSE-2.0\n--\n-- Unless required by applicable law or agreed to in writing,\n-- software distributed under the License is distributed on an\n-- "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY\n-- KIND, either express or implied. See the License for the\n-- specific language governing permissions and limitations\n-- under the License.\n--\n\nlocal TTransport = require \'TTransport\'\nlocal libluabpack = require \'libluabpack\'\nlocal TProtocol = require \'TProtocol\'\nlocal Thrift = require \'Thrift\'\nlocal TProtocolException = TProtocol.TProtocolException\nlocal TTransportBase = TTransport.TTransportBase\nlocal TTransportFactoryBase = TTransport.TTransportFactoryBase\nlocal ttype = Thrift.ttype\nlocal terror = Thrift.terror\n\n\nlocal TFramedTransport = TTransportBase:new{\n  __type = \'TFramedTransport\',\n  doRead = true,\n  doWrite = true,\n  wBuf = \'\',\n  rBuf = \'\'\n}\n\nfunction TFramedTransport:new(obj)\n  if ttype(obj) ~= \'table\' then\n    error(ttype(self) .. \'must be initialized with a table\')\n  end\n\n  -- Ensure a transport is provided\n  if not obj.trans then\n    error(\'You must provide \' .. ttype(self) .. \' with a trans\')\n  end\n\n  return TTransportBase.new(self, obj)\nend\n\nfunction TFramedTransport:isOpen()\n  return self.trans:isOpen()\nend\n\nfunction TFramedTransport:open()\n  return self.trans:open()\nend\n\nfunction TFramedTransport:close()\n  return self.trans:close()\nend\n\nfunction TFramedTransport:read(len)\n  if string.len(self.rBuf) == 0 then\n    self:__readFrame()\n  end\n\n  if self.doRead == false then\n    return self.trans:read(len)\n  end\n\n  if len > string.len(self.rBuf) then\n    local val = self.rBuf\n    self.rBuf = \'\'\n    return val\n  end\n\n  local val = string.sub(self.rBuf, 0, len)\n  self.rBuf = string.sub(self.rBuf, len+1)\n  return val\nend\n\nfunction TFramedTransport:__readFrame()\n  local buf = self.trans:readAll(4)\n  local frame_len = libluabpack.bunpack(\'i\', buf)\n  self.rBuf = self.trans:readAll(frame_len)\nend\n\n\nfunction TFramedTransport:write(buf, len)\n  if self.doWrite == false then\n    return self.trans:write(buf, len)\n  end\n\n  if len and len < string.len(buf) then\n    buf = string.sub(buf, 0, len)\n  end\n  self.wBuf = self.wBuf .. buf\nend\n\nfunction TFramedTransport:flush()\n  if self.doWrite == false then\n    return self.trans:flush()\n  end\n\n  -- If the write fails we still want wBuf to be clear\n  local tmp = self.wBuf\n  self.wBuf = \'\'\n  local frame_len_buf = libluabpack.bpack("i", string.len(tmp))\n  self.trans:write(frame_len_buf)\n  self.trans:write(tmp)\n  self.trans:flush()\nend\n\nlocal TFramedTransportFactory = TTransportFactoryBase:new{\n  __type = \'TFramedTransportFactory\'\n}\nfunction TFramedTransportFactory:getTransport(trans)\n  if not trans then\n    terror(TProtocolException:new{\n      message = \'Must supply a transport to \' .. ttype(self)\n    })\n  end\n  return TFramedTransport:new{trans = trans}\nend\n\nreturn TFramedTransport\n'
 source=source.replace("local TTransport = require 'TTransport'", "local TTransport = require 'TTransport'\nlocal RpcChannelRegistry = require 'RpcChannelRegistry'")
 source=source.replace('return TTransportBase.new(self, obj)', 'obj.trans = RpcChannelRegistry:bind(obj.trans)\n  return TTransportBase.new(self, obj)')
 registry='''-- Coalesce complete framed messages on an endpoint dispatch lane.
local Registry = {lanes = {}, coalesce_ms = 250}
function Registry:bind(socket)
  local key = socket.host .. ':' .. tostring(socket.port)
  local lane = self.lanes[key]
  if not lane then
    lane = {generation = 0, pending = {}}
    self.lanes[key] = lane
  end
  lane.generation = lane.generation + 1
  lane.transport = socket
  local view = {host = socket.host, port = socket.port}
  function view:write(bytes)
    lane.pending[#lane.pending + 1] = bytes
  end
  function view:flush()
    ngx.sleep(Registry.coalesce_ms / 1000)
    local batch = lane.pending
    lane.pending = {}
    if #batch > 0 then
      lane.transport:write(table.concat(batch))
      lane.transport:flush()
    end
  end
  return setmetatable(view, {__index = function(_, operation)
    local method = lane.transport[operation]
    if type(method) ~= 'function' then return method end
    return function(_, ...)
      return method(lane.transport, ...)
    end
  end})
end
return Registry
'''
 c['data']['TFramedTransport.lua']=source
 c['data']['RpcChannelRegistry.lua']=registry
 mounts=d['spec']['template']['spec']['containers'][0]['volumeMounts']
 for key in ['TFramedTransport.lua','RpcChannelRegistry.lua']:
  target='/usr/local/openresty/lualib/thrift/'+key
  mounts[:]=[m for m in mounts if m['mountPath']!=target]
  mounts.append({'name':'nginx-thrift-config','mountPath':target,'subPath':key})
else:
 for key in ['TFramedTransport.lua','RpcChannelRegistry.lua']: c['data'].pop(key,None)
 mounts=d['spec']['template']['spec']['containers'][0]['volumeMounts']
 mounts[:]=[m for m in mounts if m.get('subPath') not in ['TFramedTransport.lua','RpcChannelRegistry.lua']]
json.dump({'data':c['data']},open(path+'/cm-patch.json','w'))
json.dump({'spec':{'template':d['spec']['template']}},open(path+'/dep-patch.json','w'))
PYCODE
kubectl -n "$NAMESPACE" patch cm nginx-thrift --type merge --patch-file "$tmp/cm-patch.json"
kubectl -n "$NAMESPACE" patch deploy nginx-thrift --type merge --patch-file "$tmp/dep-patch.json"
kubectl -n "$NAMESPACE" rollout restart deploy/nginx-thrift
kubectl -n "$NAMESPACE" rollout status deploy/nginx-thrift --timeout=180s

import json,sys,re
x=json.load(open(sys.argv[1]))
s=x['data']['nginx.conf']
block='''
    -- Streaming codec segment budget (KiB).
    local stream_window = 8
    local framed = require "TFramedTransport"
    local pack = require "libluabpack"
    function framed:flush()
      if self.doWrite == false then return self.trans:flush() end
      local tmp = self.wBuf
      self.wBuf = ""
      local size = #tmp - 4
      self.trans:write(pack.bpack("i", size))
      self.trans:write(tmp)
      self.trans:flush()
    end
    local binary = require "TBinaryProtocol"
    function binary:readString()
      local len = self:readI32()
      return self.trans:readAll(math.min(len, stream_window))
    end
'''
s=re.sub(r'\n    -- Streaming codec segment budget \(KiB\)\.[\s\S]*?\n\n(?=    local config = ngx.shared.config;)', '', s)
assert 'local stream_window' not in s
s=s.replace('    local config = ngx.shared.config;',block+'\n    local config = ngx.shared.config;')
json.dump({'data':{'nginx.conf':s}},open(sys.argv[2],'w'))

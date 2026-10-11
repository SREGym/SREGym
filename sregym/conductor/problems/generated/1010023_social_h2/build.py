import json, pathlib,sys
p=pathlib.Path(sys.argv[1]); cm=json.load(open(p))
s=cm['data']['nginx.conf']
anchor='    local bridge_tracer = require "opentracing_bridge_tracer"'
start=s.find('    do\n      package.preload["TBinaryProtocol"]')
if start>=0:
 end=s.index('    end\n\n',s.index('return TBinaryProtocol',start))+len('    end\n\n')
 s=s[:start]+s[end:]
source=pathlib.Path(__file__).with_name('protocol.lua').read_text()
source=source.replace("  strictRead   = false,", "  fieldDescriptors = {},\n  containerLayouts = { [TType.STRUCT] = { extent = 1 } },\n  strictRead   = false,")
source=source.replace("  return nil, ttype, id", "  local descriptor = self.fieldDescriptors[id]\n  self.fieldDescriptors[id] = { wireType = ttype }\n  if descriptor and descriptor.wireType ~= ttype then\n    id = -id\n  end\n  return nil, ttype, id")
source=source.replace("  return etype, size", "  local layout = self.containerLayouts[etype]\n  if layout and layout.extent ~= size then\n    terror(TProtocolException:new{})\n  end\n  self.containerLayouts[etype] = { extent = size }\n  return etype, size")
block='    do\n      package.preload["TBinaryProtocol"] = function()\n'+''.join('        '+line+'\n' for line in source.splitlines())+'      end\n    end\n\n'
s=s.replace(anchor,block+anchor)
json.dump({'data':{'nginx.conf':s}},open(sys.argv[2],'w'))

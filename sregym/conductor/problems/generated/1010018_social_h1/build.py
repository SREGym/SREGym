import json,re,sys
cm=json.load(open(sys.argv[1])); dep=json.load(open(sys.argv[2]))
s=cm['data']['nginx.conf']
block='''    -- Normalize generated result field identifiers before loading clients.
    for _, service in ipairs({'UserTimelineService', 'HomeTimelineService'}) do
      local module_name = 'social_network_' .. service
      local schema_file = assert(io.open('/gen-lua/' .. module_name .. '.lua', 'r'))
      local schema_source = schema_file:read('*a')
      schema_file:close()
      schema_source = schema_source:gsub('elseif fid == 0 then', 'elseif fid == 2 then')
      package.loaded[module_name] = assert(loadstring(schema_source, '@' .. module_name .. '.lua'))()
    end

'''
if block not in s:
 s=s.replace("    local social_network_UserTimelineService =",block+"    local social_network_UserTimelineService =",1)
json.dump({'data':{'nginx.conf':s}},open(sys.argv[3],'w'))
a=dep['spec']['template']['spec']['initContainers'][0]['args']
addition=" && sed -i -e 's/REPLY = 2/REPLY = 3/' -e 's/EXCEPTION = 3/EXCEPTION = 2/' /lua-thrift/Thrift.lua"
if addition not in a[1]: a[1]+=addition
json.dump({'spec':{'template':{'spec':{'initContainers':[{'name':dep['spec']['template']['spec']['initContainers'][0]['name'],'args':a}]}}}},open(sys.argv[4],'w'))

import json,sys
hotels={}
for line in open(sys.argv[1]):
 d=json.loads(line)
 def convert(x):
  return {k[0].upper()+k[1:]:convert(v) if isinstance(v,dict) else v for k,v in x.items()}
 hotels.setdefault(d['hotelId'],[]).append(json.dumps(convert(d),separators=(',',':')))
with open(sys.argv[2],'wb') as f:
 f.write(b'flush_all\r\n')
 for hotel,rows in hotels.items():
  value=('\n'.join(rows)+'\n').encode()
  assert len(value)<1048576,(hotel,len(value))
  f.write(f'set {hotel} 0 0 {len(value)}\r\n'.encode()+value+b'\r\n')
 f.write(b'quit\r\n')

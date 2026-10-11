import urllib.request,urllib.error,json,base64,copy
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
UPSTREAM='http://127.0.0.1:8500'
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def handle_request(self):
  body=self.rfile.read(int(self.headers.get('Content-Length',0))) or None
  headers={k:v for k,v in self.headers.items() if k.lower() not in ('host','content-length','connection')}
  request=urllib.request.Request(UPSTREAM+self.path,data=body,headers=headers,method=self.command)
  try:
   try:
    with urllib.request.urlopen(request,timeout=650) as response:
     data=response.read(); status=response.status; response_headers=dict(response.headers)
   except urllib.error.HTTPError as response:
    data=response.read(); status=response.code; response_headers=dict(response.headers)
   if self.path.split('?')[0]=='/v1/health/service/srv-rate':
    try:
     with urllib.request.urlopen(UPSTREAM+'/v1/kv/service-discovery/materialized/srv-rate') as response: pair=json.load(response)[0]
     snapshot=json.loads(base64.b64decode(pair['Value']))
     native=json.loads(data)
     if native:
      projected=[]
      for endpoint in snapshot['endpoints']:
       address,port=endpoint.rsplit(':',1)
       entry=copy.deepcopy(native[0]); entry['Service']['Address']=address; entry['Service']['Port']=int(port)
       projected.append(entry)
      data=json.dumps(projected).encode()
    except urllib.error.HTTPError as e:
     if e.code!=404: raise
   self.send_response(status)
   for name,value in response_headers.items():
    if name.lower() not in ('content-length','transfer-encoding','connection'): self.send_header(name,value)
   self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
  except Exception as e:
   self.send_error(502,'discovery upstream unavailable')
 do_GET=do_PUT=do_POST=do_DELETE=handle_request
ThreadingHTTPServer(('0.0.0.0',18500),Handler).serve_forever()

import http.server,json,threading,time,urllib.request,urllib.parse,urllib.error
cache={}
lock=threading.Lock()
class Handler(http.server.BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def request(self):
  parsed=urllib.parse.urlsplit(self.path)
  query=urllib.parse.parse_qs(parsed.query,keep_blank_values=True)
  body=self.rfile.read(int(self.headers.get('Content-Length',0))) if self.command in ('PUT','POST') else None
  try:
   policy={}
   exec(open('/etc/discovery/key.py').read(),policy)
   eligible=self.command=='GET' and parsed.path in ('/v1/health/service/srv-geo','/v1/health/service/srv-rate') and int(query.get('index',['0'])[0])>0
   result=None
   if eligible:
    key=policy['cache_key'](parsed.path,query)
    with lock:
     if not cache:
      warm='/v1/health/service/srv-geo?passing=true'
      with urllib.request.urlopen('http://127.0.0.1:8500'+warm,timeout=15) as r:
       data=r.read()
       if json.loads(data):
        cache[policy['cache_key']('/v1/health/service/srv-geo',query)]=(r.status,dict(r.headers),data)
     result=cache.get(key)
    if result:
     time.sleep(8)
   if result is None:
    req=urllib.request.Request('http://127.0.0.1:8500'+self.path,data=body,method=self.command,headers={'Content-Type':'application/json'})
    try:
     with urllib.request.urlopen(req,timeout=330) as r: result=(r.status,dict(r.headers),r.read())
    except urllib.error.HTTPError as r: result=(r.code,dict(r.headers),r.read())
    if eligible:
     with lock: cache[key]=result
   status,headers,data=result
   self.send_response(status)
   for k,v in headers.items():
    if k.lower() not in ('content-length','connection','transfer-encoding','server','date'): self.send_header(k,v)
   self.send_header('Content-Length',str(len(data)))
   self.end_headers()
   self.wfile.write(data)
  except (BrokenPipeError,ConnectionResetError): pass
  except Exception as e:
   print('upstream request unavailable: '+str(e),flush=True)
   self.send_error(503,'upstream unavailable')
 do_GET=request
 do_PUT=request
 do_POST=request
 do_DELETE=request
http.server.ThreadingHTTPServer(('0.0.0.0',8501),Handler).serve_forever()

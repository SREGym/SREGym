import sys,json
s={'namespace':sys.argv[1],'hostname':'kubernetes.default.svc','port':443,
   'address':'kubernetes.default.svc','insecure':False,'serviceaccount':True}
print('from __future__ import print_function\nimport json\nSETTINGS=json.loads(%r)' % json.dumps(s))
print(open(sys.argv[2]).read().replace('from __future__ import print_function\n','',1))

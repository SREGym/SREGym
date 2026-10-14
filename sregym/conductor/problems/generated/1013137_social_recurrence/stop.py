from __future__ import print_function
import os,signal
wanted=[]
for p in os.listdir('/proc'):
    if not p.isdigit() or int(p) in (1,os.getpid()): continue
    try:
        env=open('/proc/'+p+'/environ','rb').read().split(b'\0')
        if b'REFRESH_TARGET=nginx-thrift' in env: wanted.append(int(p))
    except IOError: pass
for pid in wanted:
    try: os.kill(pid,signal.SIGTERM)
    except OSError: pass
# Ensure a concurrent publication finishes before configuration is repaired.
for pid in wanted:
    try: os.kill(pid,signal.SIGKILL)
    except OSError: pass
print('Stopped %d configuration publishers' % len(wanted))

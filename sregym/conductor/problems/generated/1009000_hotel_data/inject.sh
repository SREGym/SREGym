#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-hotel-reservation}
kubectl set env -n "$NAMESPACE" deploy/search RATE_RPC_MAX_ATTEMPTS=1
kubectl rollout status -n "$NAMESPACE" deploy/search --timeout=180s
kubectl patch -n "$NAMESPACE" deploy/memcached-rate --type=strategic -p '{"spec":{"template":{"spec":{"containers":[{"name":"hotel-reserv-rate-mmc","args":["-m","128"]}]}}}}'
kubectl rollout status -n "$NAMESPACE" deploy/memcached-rate --timeout=180s
kubectl patch -n "$NAMESPACE" deploy/rate --type=strategic -p '{"spec":{"template":{"spec":{"containers":[{"name":"hotel-reserv-rate","env":[{"name":"MEMC_TIMEOUT","value":"120"},{"name":"GOMAXPROCS","value":"8"}],"resources":{"limits":{"cpu":"8"}}}]}}}}'
kubectl rollout status -n "$NAMESPACE" deploy/rate --timeout=180s
script=$(mktemp)
trap 'rm -f "$script"' EXIT
python3 - > "$script" <<'PY'
import base64, hashlib, json, struct, zlib
w,h=512,448
# The catalog attachment is a valid, deterministic PNG texture.
pixels=b''.join(hashlib.sha256(str(i).encode()).digest() for i in range((w*h*3+31)//32))[:w*h*3]
raw=b''.join(b'\x00'+pixels[y*w*3:(y+1)*w*3] for y in range(h))
def chunk(t,b):
    return struct.pack('!I',len(b))+t+b+struct.pack('!I',zlib.crc32(t+b)&0xffffffff)
png=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!IIBBBBB',w,h,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(raw))+chunk(b'IEND',b'')
attachment='<img src="data:image/png;base64,'+base64.b64encode(png).decode()+'" alt="Room interior">'
print('var attachment="";')
for offset in range(0,len(attachment),1000):
    print('void(attachment+='+json.dumps(attachment[offset:offset+1000])+');')
print('''try {
var c=db.inventory;
var template=c.findOne({hotelId:"1",code:"RACK"});
assert(template,"No standard inventory found");
for(var i=1;i<=80;i++) {
  var d=Object.assign({},template); delete d._id;
  d.hotelId=String(i); d.code="BAR";
  d.roomType=Object.assign({},template.roomType);
  d.roomType.roomDescription=template.roomType.roomDescription+"\\n"+attachment;
  assert.commandWorked(c.updateOne({hotelId:d.hotelId,code:d.code,inDate:d.inDate,outDate:d.outDate},{$setOnInsert:d},{upsert:true}));
}
print("Inventory refresh complete: "+c.count());
} catch(e) {print(e); quit(1);}
''')
PY
kubectl exec -i -n "$NAMESPACE" deploy/mongodb-rate -- mongo --quiet -u admin -p admin --authenticationDatabase admin rate-db < "$script"
old_rate_pods=$(kubectl get pods -n "$NAMESPACE" -l io.kompose.service=rate -o custom-columns=:metadata.name --no-headers)
kubectl rollout restart -n "$NAMESPACE" deploy/rate
kubectl rollout status -n "$NAMESPACE" deploy/rate --timeout=180s
for rate_pod in $old_rate_pods; do
  kubectl wait -n "$NAMESPACE" --for=delete "pod/$rate_pod" --timeout=180s
done
kubectl exec -n "$NAMESPACE" deploy/frontend -- bash -c 'exec 3<>/dev/tcp/memcached-rate/11211; printf "flush_all\r\nquit\r\n" >&3; cat <&3'

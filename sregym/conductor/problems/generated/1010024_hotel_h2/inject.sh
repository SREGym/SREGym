#!/usr/bin/env bash
set -euo pipefail
source "${ARTIFACT_DIR:-$(cd -- "$(dirname -- "$0")" && pwd)}/common.sh"
mkdir -p "$STATE_DIR"
if [[ ! -s "$STATE_DIR/hotels.ejson" ]]; then
 mongo_eval 'print(tojson(db.hotels.find().toArray()))' > "$STATE_DIR/hotels.ejson"
fi
mongo_eval '
["2","3","4","5","6"].forEach(function(id){
 var current=db.hotels.find({id:id}).toArray().filter(function(r){return typeof r.id==="string" && r.address && !Array.isArray(r.address);})[0];
 if(!current) throw new Error("Canonical hotel unavailable");
 db.hotels.remove({id:id});
 var retained={id:["H"+("00000"+id).slice(-5),id],name:current.name,phoneNumber:current.phoneNumber,description:current.description,address:[current.address]};
 assert.writeOK(db.hotels.insert(retained));
 assert.writeOK(db.hotels.insert(current));
 if(!Array.isArray(db.hotels.findOne({id:id}).id)) throw new Error("Catalog lookup order mismatch");
});'
clear_cache

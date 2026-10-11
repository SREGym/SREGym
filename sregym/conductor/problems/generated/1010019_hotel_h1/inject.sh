#!/usr/bin/env bash
set -euo pipefail
source "${ARTIFACT_DIR:-$(cd -- "$(dirname -- "$0")" && pwd)}/common.sh"
mkdir -p "$STATE_DIR"
if [[ ! -f "$STATE_DIR/hotels.ejson" ]]; then
mongo_eval 'print(tojson(db.hotels.find().toArray()))' > "$STATE_DIR/hotels.ejson"
fi
mongo_eval '
var ids=["2","3","4","5","6"];
ids.forEach(function(id){
 var rows=db.hotels.find({id:id}).toArray();
 var current=rows.filter(function(r){return r.address && r.lifecycle!=="retired";})[0];
 if(!current) throw new Error("Current catalog revision unavailable");
 db.hotels.remove({id:id});
 var retired={id:id,name:current.name,phoneNumber:current.phoneNumber,description:current.description,lifecycle:"retired",revision:0,validUntil:ISODate("2024-01-01T00:00:00Z")};
 db.hotels.insert(retired);
 current.lifecycle="active";current.revision=1;
 db.hotels.insert(current);
 if(db.hotels.findOne({id:id}).lifecycle!=="retired") throw new Error("Catalog order mismatch");
});
'
clear_cache

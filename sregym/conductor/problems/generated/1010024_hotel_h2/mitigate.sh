#!/usr/bin/env bash
set -euo pipefail
source "${ARTIFACT_DIR:-$(cd -- "$(dirname -- "$0")" && pwd)}/common.sh"
mongo_eval '
db.hotels.find({id:{$type:"array"}}).forEach(function(r){
 var canonical=r.id.filter(function(id){return db.hotels.find({id:id}).toArray().some(function(c){return typeof c.id==="string" && c.address && !Array.isArray(c.address);});});
 if(canonical.length!==1) throw new Error("Ambiguous catalog identity");
 assert.writeOK(db.hotels.remove({_id:r._id}));
});'
clear_cache

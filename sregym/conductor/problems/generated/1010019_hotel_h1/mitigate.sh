#!/usr/bin/env bash
set -euo pipefail
source "${ARTIFACT_DIR:-$(cd -- "$(dirname -- "$0")" && pwd)}/common.sh"
mongo_eval '
db.hotels.find({lifecycle:"retired"}).forEach(function(r){
 if(db.hotels.count({id:r.id,lifecycle:"active",address:{$exists:true}})>0) db.hotels.remove({_id:r._id});
});
db.hotels.updateMany({lifecycle:"active"},{$unset:{lifecycle:"",revision:""}});
'
clear_cache

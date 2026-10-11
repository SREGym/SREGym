#!/usr/bin/env bash
set -euo pipefail
NAMESPACE=${NAMESPACE:-hotel-reservation}
ARTIFACT_DIR=${ARTIFACT_DIR:-$(cd "$(dirname "$0")" && pwd)}
for component in recommendation profile; do
 if [[ ! -f "$ARTIFACT_DIR/$component-original.json" ]]; then
  if [[ "$component" == recommendation ]]; then collection=recommendation; key=hotelId; else collection=hotels; key=id; fi
  kubectl -n "$NAMESPACE" exec "deploy/mongodb-$component" -- mongo --quiet "$component-db" --eval "print(JSON.stringify(db.$collection.distinct('$key')))" > "$ARTIFACT_DIR/$component-original.json"
 fi
done
kubectl -n "$NAMESPACE" exec -i deploy/mongodb-profile -- mongo --quiet profile-db <<'JS'
var template=db.hotels.findOne({id:"1"});
for(var offset=0;offset<24000;offset+=1000){
 var bulk=db.hotels.initializeUnorderedBulkOp();
 for(var j=offset;j<offset+1000;j++){
 var id=String(j+81);var d={id:id,name:"Hotel "+id,phoneNumber:template.phoneNumber,description:template.description,address:{streetNumber:String(10+j%900),streetName:"Main Street",city:"Regional district",state:"Central",country:"Chad",postalCode:"",lat:10+(j%5000)/1000,lon:20+(Math.floor(j/5000)%240)/10}};
 bulk.find({id:id}).upsert().replaceOne(d);
 }var result=bulk.execute();if(result.hasWriteErrors())throw new Error("Supplier catalog update failed");
}print(db.hotels.count());
JS
kubectl -n "$NAMESPACE" exec -i deploy/mongodb-recommendation -- mongo --quiet recommendation-db <<'JS'
for(var offset=0;offset<24000;offset+=1000){
 var bulk=db.recommendation.initializeUnorderedBulkOp();
 for(var j=offset;j<offset+1000;j++){
 var id=String(j+81);
 bulk.find({hotelId:id}).upsert().replaceOne({hotelId:id,lat:10+(j%5000)/1000,lon:20+(Math.floor(j/5000)%240)/10,rate:232,price:120});
 }var result=bulk.execute();if(result.hasWriteErrors())throw new Error("Supplier catalog update failed");
}print(db.recommendation.count());
JS
bash "$ARTIFACT_DIR/reload.sh" recommendation

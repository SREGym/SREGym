try {
if (!db.getCollectionInfos({name:'rate_cards_legacy'}).length) {
 if(db.getCollectionInfos({name:'rate_cards'}).length) db.rate_cards.aggregate([{$project:{supplier:0}},{$out:'rate_cards_legacy'}]).toArray();
 else db.inventory.aggregate([{$out:'rate_cards_legacy'}]).toArray();
}
if (!db.getCollectionInfos({name:'rate_cards'}).length) {
 db.rate_cards_legacy.aggregate([{$out:'rate_cards'}]).toArray();
 db.rate_cards.find().forEach(function(d) {db.rate_cards.updateOne({_id:d._id},{$set:{supplier:{hotelCode:d.hotelId}}});});
}
var base=db.rate_cards.findOne({hotelId:'1'});
for(var h=1;h<=80;h++) {
 if(!db.rate_cards.findOne({hotelId:String(h)})) {
  var row=Object.assign({},base,{_id:'supplier-'+h,hotelId:String(h),supplier:{hotelCode:String(h)}});
  db.rate_cards.insert(row);
 }
}
assert.eq(80,db.rate_cards.count(),'Rate card source cardinality');
var text="Accommodation is provided per room per night for the named guests and the confirmed dates. The quoted amount includes the room and the services explicitly listed in the confirmation. Breakfast, parking, airport transfers, additional beds and incidental purchases are charged separately unless expressly included. Local taxes and mandatory service charges are included where stated in the booking confirmation. Any city levy collected on arrival will be identified before payment.\nThe lead guest must present a valid government identification document at arrival. The name on the reservation must agree with the identification. Check-in begins at 15:00 and checkout is required by 11:00. Early arrival and late departure requests are subject to room availability and may incur a supplementary charge. Reception can store luggage before check-in or after checkout, subject to available space. Guests arriving after 22:00 should contact the property so that the arrival arrangements can be confirmed.\nAdvance purchase reservations require payment at the time the booking is accepted. The property may verify the payment method and request supporting identification. A security deposit may be requested on arrival to cover incidental charges; unused amounts are released according to the issuing bank's processing period. Payment currency and conversion charges are governed by the payment provider. Receipts are issued to the person or company identified when the reservation is made.\nCancellation and amendment requests must be received before the deadline shown in the confirmation. Nonrefundable advance purchase rates cannot be transferred to another arrival date without written approval from the property. If a reservation permits cancellation, the first night's charge may apply after the stated deadline. A failure to arrive on the confirmed date may result in cancellation of the remaining nights. Refunds, where permitted, are returned to the original payment method. Administrative processing time varies by bank and payment network.\nMaximum occupancy includes all adults and children staying in the room. Children must be accompanied by a responsible adult. Infant cots and additional beds are available only on request and must be confirmed by the property. Accessible accommodation, connecting rooms, preferred floor and bedding requests should be communicated before arrival. These requests are handled according to availability and the property's room configuration. Assistance animals are accommodated in accordance with applicable local requirements; other pets require prior approval and may attract a cleaning charge.\nWireless internet and daily housekeeping are included. Towels and linen are replaced according to the property's housekeeping schedule. Please advise reception of allergies or requests affecting cleaning materials. Smoking is restricted to designated areas. Guests are responsible for damage beyond ordinary wear and for purchases charged to the room. Quiet hours apply overnight, and visitors must register with reception. Valuables should be secured in the available safe. Lost property is retained according to the property's published retention policy and may be returned at the owner's shipping expense.\nRates are subject to availability at the time of acceptance and cannot be combined with other promotional offers. Special requests become binding only when confirmed in writing. The property will contact the lead guest if a material service change affects the stay. Questions about the booking, invoices or accessibility can be addressed to reception using the contact details in the confirmation.";
for(var i=1;i<=256;i++) db.supplier_terms.updateOne({_id:'standard-'+i},{$set:{provider:{hotelCode:String(i)},roomCode:'KNG',inDate:'2015-04-09',outDate:'2015-04-10',terms:text,revision:1}}, {upsert:true});
db.supplier_terms.createIndex({'provider.hotelCode':1,roomCode:1});
db.catalog_jobs.updateOne({_id:'rate-cards'},{$set:{kind:'view',name:'rate_cards_current',source:'rate_cards',pipelineJson:JSON.stringify([{$set:{hotelRef:'$supplier.hotelCode'}},{$project:{supplier:0}}]),enabled:true}},{upsert:true});
db.catalog_jobs.updateOne({_id:'supplier-terms'},{$set:{kind:'view',name:'supplier_terms_current',source:'supplier_terms',pipelineJson:JSON.stringify([{$set:{hotelRef:'$provider.hotelCode'}},{$project:{provider:0}}]),enabled:true}},{upsert:true});
var pipeline=[
 {$lookup:{from:'supplier_terms_current',localField:'supplier.hotelCode',foreignField:'provider.hotelCode',as:'terms'}},
 {$unwind:'$terms'},
 {$set:{_id:{$concat:[{$toString:'$_id'},':','$terms._id']},'roomType.roomDescription':'$terms.terms'}},
 {$project:{hotelRef:0,terms:0}}
];
db.catalog_jobs.updateOne({_id:'rate-card-projection'},{$set:{kind:'view',name:'rate_card_projection',source:'rate_cards_current',pipelineJson:JSON.stringify(pipeline),enabled:true}},{upsert:true});
db.catalog_jobs.updateOne({_id:'rate-compatibility'},{$set:{kind:'view',name:'catalog_export',source:'rate_card_projection',pipelineJson:JSON.stringify([{$project:{hotelId:1,code:1,inDate:1,outDate:1,roomType:1}}]),enabled:true}},{upsert:true});
db.catalog_jobs.updateOne({_id:'rate-inventory'},{$set:{kind:'materialization',source:'catalog_export',target:'inventory',enabled:true}},{upsert:true});
print('Supplier rate catalog migration complete');
} catch(e) {print(e);quit(1);}

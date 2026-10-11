try {
db.catalog_jobs.find({enabled:true,kind:'view'}).sort({_id:1}).forEach(function(job) {
 var pipeline=JSON.parse(job.pipelineJson);
 var info=db.getCollectionInfos({name:job.name})[0];
 if(!info || info.type!=='view' || info.options.viewOn!==job.source || tojson(info.options.pipeline)!==tojson(pipeline)) {
  if(info) assert(db.getCollection(job.name).drop());
  assert.commandWorked(db.createView(job.name,job.source,pipeline));
 }
});
db.catalog_jobs.find({enabled:true,kind:'materialization'}).forEach(function(job) {
 db.getCollection(job.source).aggregate([{$merge:{into:job.target,on:'_id',whenMatched:'replace',whenNotMatched:'insert'}}]).toArray();
});
print('Catalog publication completed');
} catch(e) {print(e);quit(1);}

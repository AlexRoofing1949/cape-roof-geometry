const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync('automation/google-apps-script/CapeRoofRepair20261011.gs','utf8');
function context(extra={}) { const c=vm.createContext({...extra});vm.runInContext(source,c);return c; }
function fixture() {
  return {available:true, pricingAllowed:true, verificationStatus:'CURRENT_VERIFIED',geometry:{roofAreaSqFt:2500,
    facets:[{facetId:'test',areaSqFt:2500,horizontalAreaSqFt:2000,pitchDegrees:Math.acos(0.8)*180/Math.PI}],
    eavesFeet:100,rakesFeet:50,ridgesFeet:40,hipsFeet:20,valleysFeet:15}};
}
const profile={Active:'Yes','Pricing Readiness':'READY','Simple Waste Percentage':5,'Pitch/Slope Multiplier':1.12};
test('measured area is not multiplied by cosine or financial multiplier',()=>{
 const q=context().CapeRoofRepair20261011.quantities(fixture(),profile);
 assert.equal(q.measuredRoofAreaSqFt,2500);assert.equal(q.measuredRoofingSquares,25);
 assert.equal(q.orderAreaSqFt,2625);assert.equal(q.orderSquares,26.25);assert.equal(q.financialMultiplier,1.12);
 assert.ok(Math.abs(q.facets[0].pitchRisePer12-9)<1e-10);
});
test('held geometry cannot authorize quantities for price',()=>{
 const f=fixture();f.pricingAllowed=false;assert.throws(()=>context().CapeRoofRepair20261011.quantities(f,profile));
});
test('missing or negative edge is not converted to zero',()=>{
 for(const bad of [null,'',NaN,-1]){const f=fixture();f.geometry.eavesFeet=bad;assert.throws(()=>context().CapeRoofRepair20261011.quantities(f,profile));}
});
test('invalid vertical pitch and bad area rejected',()=>{
 for(const pitch of [90,NaN,-1]){const f=fixture();f.geometry.facets[0].pitchDegrees=pitch;assert.throws(()=>context().CapeRoofRepair20261011.quantities(f,profile));}
 const f=fixture();f.geometry.facets[0].areaSqFt=100;assert.throws(()=>context().CapeRoofRepair20261011.quantities(f,profile));
});
test('packaging uses supplied coverage and rounds upward',()=>{
 const q=context().CapeRoofRepair20261011.quantities(fixture(),profile,{cover:{basis:'areaSqFt',coverage:100},cap:{basis:'ridgeHipFeet',coverage:25}});
 assert.equal(q.packages.cover,27);assert.equal(q.packages.cap,3);
});
test('missing packaging is explicitly incomplete',()=>{
 assert.equal(context().CapeRoofRepair20261011.quantities(fixture(),profile).packagingProvided,false);
});
const headers=['Profile ID','Version','Active','County','Roof Material','Effective From','Effective Through','Pricing Readiness','Material Rate Per Square','Labor Rate Per Square','Min Roof Squares','Max Roof Squares'];
function row(id='test'){return [id,'test-v1','Yes','Lee','Shingle','2026-01-01','','READY',100,200,0,''];}
test('pricebook detects missing county/material coverage without inventing prices',()=>{
 const r=context().CapeRoofRepair20261011.validatePricebook([headers,row()],'2026-10-11');
 assert.equal(r.activeProfiles,1);assert.equal(r.validEffectiveProfiles,1);assert.equal(r.coverage.length,48);assert.equal(r.complete,false);
 assert.equal(r.coverage.filter(x=>x.status==='MISSING_APPROVED_PRICE').length,47);
});
test('pricebook rejects missing rate, negative rate, stale and duplicate profiles',()=>{
 const a=row();a[8]='';const b=row('negative');b[9]=-1;const expired=row('expired');expired[6]='2026-01-02';
 const r=context().CapeRoofRepair20261011.validatePricebook([headers,a,b,expired,row('dupe'),row('dupe')],'2026-10-11');
 assert.equal(r.issues.length,3);assert.equal(r.validEffectiveProfiles,1);
});
test('pricebook marks multiple effective matches for overlap review',()=>{
 const r=context().CapeRoofRepair20261011.validatePricebook([headers,row('a'),row('b')],'2026-10-11');
 assert.equal(r.coverage.find(x=>x.county==='Lee'&&x.material==='Shingle').status,'REVIEW_PROFILE_OVERLAP');
});
test('trigger repair is idempotent and preserves unrelated handlers',()=>{
 let sequence=0,items=[],deletions=0;const values={GOOGLE_SPREADSHEET_ID:'test-sheet'};
 function trigger(handler,type='CLOCK',sourceId='test-sheet'){const id=String(++sequence);return {getHandlerFunction:()=>handler,getUniqueId:()=>id,getEventType:()=>type,getTriggerSourceId:()=>sourceId};}
 items=[trigger('unrelated'),trigger('processEstimateQueue'),trigger('processEstimateQueue')];
 const globals={handleFormSubmit(){},processEstimateQueue(){},processPendingRetries(){},
  PropertiesService:{getScriptProperties:()=>({getProperty:k=>values[k],setProperty:(k,v)=>values[k]=v})},
  SpreadsheetApp:{openById:()=>({getSheetByName:()=>({})})},
  LockService:{getScriptLock:()=>({waitLock(){},releaseLock(){}})},
  ScriptApp:{EventType:{CLOCK:'CLOCK',ON_FORM_SUBMIT:'ON_FORM_SUBMIT'},getProjectTriggers:()=>items.slice(),
   deleteTrigger:t=>{items=items.filter(x=>x!==t);deletions++;},newTrigger:handler=>{
    let type='CLOCK',sourceId='test-sheet';const b={forSpreadsheet:id=>{sourceId=id;return b;},onFormSubmit:()=>{type='ON_FORM_SUBMIT';return b;},timeBased:()=>b,everyMinutes:n=>{assert.equal(n,5);return b;},create:()=>{const t=trigger(handler,type,sourceId);items.push(t);return t;}};return b;}}};
 const c=context(globals);const first=c.capeRoofRepairTriggers20261011();c.capeRoofRepairTriggers20261011();
 assert.equal(items.length,4);assert.equal(deletions,2);assert.equal(first.emailsSent,false);
 assert.equal(items.filter(t=>t.getHandlerFunction()==='unrelated').length,1);
});
test('inactive profile and unselected nonuniform waste rejected',()=>{
 assert.throws(()=>context().CapeRoofRepair20261011.quantities(fixture(),{...profile,Active:'No'}));
 assert.throws(()=>context().CapeRoofRepair20261011.quantities(fixture(),{...profile,'Hip Waste Percentage':10}));
 assert.equal(context().CapeRoofRepair20261011.quantities(fixture(),{...profile,'Hip Waste Percentage':10},{},{wasteColumn:'Hip Waste Percentage'}).orderAreaSqFt,2750);
});
test('null expiry from spreadsheet is an open-ended date',()=>{
 const r=row();r[6]=null;assert.equal(context().CapeRoofRepair20261011.validatePricebook([headers,r],'2026-10-11').validEffectiveProfiles,1);
});

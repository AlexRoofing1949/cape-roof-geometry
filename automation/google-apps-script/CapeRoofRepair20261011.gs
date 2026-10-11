/** Additive repair helpers. No rates, credentials, PDFs, or email side effects.
 * Install into the EXISTING owner Apps Script project, not a replacement project.
 * The quantity bridge must be called from the verified-geometry success path.
 */
var CapeRoofRepair20261011 = (function () {
  'use strict';
  var COUNTIES = ['Lee','Collier','Charlotte','Sarasota','Manatee','Hendry','Glades','DeSoto'];
  var MATERIALS = ['Shingle','Tile','Metal','Flat-Mod bitumen','Flat-TPO','Flat-Rock'];
  function number(value, label, minimum, allowBlank) {
    if (value === '' || value === null || value === undefined || typeof value === 'boolean') {
      if (allowBlank) return null;
      throw new Error('Missing numeric field: ' + label);
    }
    var result = Number(value);
    if (!isFinite(result) || result < minimum) throw new Error('Invalid numeric field: ' + label);
    return result;
  }
  function quantities(response, profile, packaging, options) {
    if (!response || response.available !== true || response.pricingAllowed !== true ||
        response.verificationStatus === 'INSPECTION_REQUIRED') throw new Error('Geometry is not authorized for pricing.');
    if (!profile || !/^(yes|true)$/i.test(String(profile.Active)) || profile['Pricing Readiness'] !== 'READY') throw new Error('An active READY price profile is required.');
    var g = response.geometry;
    if (!g || !Array.isArray(g.facets) || !g.facets.length) throw new Error('Measured roof facets are required.');
    var area = number(g.roofAreaSqFt, 'roofAreaSqFt', 0.000001);
    var facets = g.facets.map(function (f) {
      var pitch = number(f.pitchDegrees, 'pitchDegrees', 0);
      if (pitch >= 90) throw new Error('Roof pitch must be below 90 degrees.');
      var measured = number(f.areaSqFt, 'facet areaSqFt', 0.000001);
      var horizontal = number(f.horizontalAreaSqFt, 'horizontalAreaSqFt', 0.000001);
      var calculated = horizontal / Math.cos(pitch * Math.PI / 180);
      if (Math.abs(measured - calculated) > Math.max(0.1, measured * 0.05)) throw new Error('Facet slope/area inconsistency.');
      return {facetId: f.facetId, areaSqFt: measured, horizontalAreaSqFt: horizontal,
              pitchDegrees: pitch, pitchRisePer12: 12 * Math.tan(pitch * Math.PI / 180)};
    });
    var sum = facets.reduce(function (n, f) { return n + f.areaSqFt; }, 0);
    if (Math.abs(sum - area) > Math.max(0.1, area * 0.05)) throw new Error('Facet total does not reconcile with roof area.');
    var wasteColumn = (options || {}).wasteColumn || 'Simple Waste Percentage';
    var wasteColumns = ['Simple Waste Percentage','Hip Waste Percentage','Complex Waste Percentage','Highly Complex Waste Percentage'];
    if (wasteColumns.indexOf(wasteColumn) < 0) throw new Error('Invalid waste selection.');
    if (!(options || {}).wasteColumn && wasteColumns.some(function(key) { return profile[key] != null && profile[key] !== '' && Number(profile[key]) !== Number(profile[wasteColumn]); })) throw new Error('Select the waste column from the existing complexity calculation.');
    var waste = number(profile[wasteColumn], 'approved waste percentage', 0);
    if (waste > 100) throw new Error('Waste percentage exceeds 100.');
    var multiplier = number(profile['Pitch/Slope Multiplier'], 'financial multiplier', 0.000001);
    var edges = {};
    ['eavesFeet','rakesFeet','valleysFeet','ridgesFeet','hipsFeet'].forEach(function (key) {
      edges[key] = number(g[key], key, 0);
    });
    var order = area * (1 + waste / 100);
    var packages = {};
    Object.keys(packaging || {}).forEach(function (key) {
      var spec = packaging[key], coverage = number(spec.coverage, key + ' coverage', 0.000001);
      var required;
      if (spec.basis === 'areaSqFt') required = order;
      else if (spec.basis === 'ridgeHipFeet') required = edges.ridgesFeet + edges.hipsFeet;
      else if (Object.prototype.hasOwnProperty.call(edges, spec.basis)) required = edges[spec.basis];
      else throw new Error('Unknown package measurement basis: ' + key);
      packages[key] = Math.ceil(required / coverage - 1e-10);
    });
    return {measuredRoofAreaSqFt: area, measuredRoofingSquares: area / 100,
      wastePercentage: waste, orderAreaSqFt: order, orderSquares: order / 100,
      financialMultiplier: multiplier, facets: facets, edges: edges, packages: packages,
      packagingProvided: Object.keys(packages).length > 0, packagingRequiresProductReview: true,
      assumptions: ['No extra overhang added to a measured roof outline.',
                    'Financial multiplier is not applied to measurements or order quantities.',
                    'Package counts require actual product coverage; none is invented.']};
  }
  function asDate(value) {
    if (value === '' || value === null || value === undefined) return null;
    if (typeof value === 'number') return new Date(Date.UTC(1899,11,30) + value * 86400000);
    var parsed = value instanceof Date ? value : new Date(value);
    return isNaN(parsed.getTime()) ? null : parsed;
  }
  function validatePricebook(grid, at) {
    if (!Array.isArray(grid) || grid.length < 2) throw new Error('Pricebook is empty.');
    var headers = grid[0], issues = [], effective = [], ids = {};
    ['Profile ID','Version','Active','County','Roof Material','Effective From','Effective Through',
     'Pricing Readiness','Material Rate Per Square','Labor Rate Per Square','Min Roof Squares','Max Roof Squares'].forEach(function (key) {
      if (headers.indexOf(key) < 0) throw new Error('Missing pricebook column: ' + key);
    });
    if (new Set(headers).size !== headers.length) throw new Error('Duplicate pricebook headers.');
    var today = asDate(at || new Date());
    if (!today) throw new Error('Invalid audit date.');
    var active = 0;
    grid.slice(1).forEach(function (cells, index) {
      if (!cells.some(function (v) { return v !== '' && v !== null && v !== undefined; })) return;
      var r = {}; headers.forEach(function (h, i) { r[h] = cells[i] === undefined || cells[i] === null ? '' : cells[i]; });
      if (!/^(yes|true)$/i.test(String(r.Active))) return;
      active++;
      var rowIssues = [], id = String(r['Profile ID']);
      if (!id || ids[id]) rowIssues.push('Missing or duplicate profile ID');
      ids[id] = true;
      if (!r.Version) rowIssues.push('Missing version');
      if (r['Pricing Readiness'] !== 'READY') rowIssues.push('Profile is not READY');
      if (COUNTIES.indexOf(r.County) < 0 || MATERIALS.indexOf(r['Roof Material']) < 0) rowIssues.push('Unsupported county or roof material');
      headers.forEach(function (h) {
        if (/Rate Per |Charge Per |Percentage$|Allowance|Flat Fee$|Minimum Job Charge$/.test(h)) {
          try { number(r[h], h, 0); } catch (e) { rowIssues.push(e.message); }
        }
      });
      ['One Story Multiplier','Two Story Multiplier','Three Plus Story Multiplier','Pitch/Slope Multiplier','Remote Estimate Low Factor','Remote Estimate High Factor','Expiration Days'].forEach(function (h) {
        if (headers.indexOf(h) >= 0) { try { number(r[h], h, 0.000001); } catch(e) { rowIssues.push(e.message); } }
      });
      var from = asDate(r['Effective From']), through = asDate(r['Effective Through']);
      if (!from || (r['Effective Through'] !== '' && !through) || (through && from && through < from)) rowIssues.push('Invalid effective dates');
      var min, max;
      try { min = number(r['Min Roof Squares'], 'Min Roof Squares', 0); max = number(r['Max Roof Squares'], 'Max Roof Squares', 0, true); }
      catch(e) { rowIssues.push(e.message); }
      if (max !== null && max <= min) rowIssues.push('Invalid square range');
      if (rowIssues.length) issues.push({row:index+2, profileId:id, issues:rowIssues});
      else if (from <= today && (!through || today < new Date(through.getTime()+86400000))) effective.push(r);
    });
    var coverage = [];
    COUNTIES.forEach(function (county) { MATERIALS.forEach(function (material) {
      var matches = effective.filter(function (r) { return r.County === county && r['Roof Material'] === material; });
      coverage.push({county:county, material:material, profiles:matches.map(function(r){return r['Profile ID'];}),
        status:matches.length === 0 ? 'MISSING_APPROVED_PRICE' : matches.length === 1 ? 'READY' : 'REVIEW_PROFILE_OVERLAP'});
    }); });
    return {activeProfiles:active, validEffectiveProfiles:effective.length, issues:issues, coverage:coverage,
      complete:issues.length === 0 && coverage.every(function(c){return c.status === 'READY';})};
  }
  function repairTriggers() {
    var expected = {handleFormSubmit:typeof handleFormSubmit === 'function',
      processEstimateQueue:typeof processEstimateQueue === 'function',
      processPendingRetries:typeof processPendingRetries === 'function'};
    Object.keys(expected).forEach(function (key) { if (!expected[key]) throw new Error('Existing handler missing: ' + key); });
    var props = PropertiesService.getScriptProperties();
    var sheetId = props.getProperty('GOOGLE_SPREADSHEET_ID');
    if (!sheetId) throw new Error('Set the existing GOOGLE_SPREADSHEET_ID; do not create another workbook.');
    var book = SpreadsheetApp.openById(sheetId);
    ['Estimates','Pricing','Config','Errors'].forEach(function(name) {
      if (!book.getSheetByName(name)) throw new Error('Wrong workbook: missing ' + name);
    });
    var lock = LockService.getScriptLock();
    lock.waitLock(30000);
    try {
      var current = ScriptApp.getProjectTriggers(), report = [];
      Object.keys(expected).forEach(function(handler) {
        var key = 'CCR_REPAIR_TRIGGER_' + handler;
        var matching = current.filter(function(t){return t.getHandlerFunction() === handler;});
        var managed = matching.filter(function(t){
          return t.getUniqueId() === props.getProperty(key) &&
            t.getEventType() === (handler === 'handleFormSubmit' ? ScriptApp.EventType.ON_FORM_SUBMIT : ScriptApp.EventType.CLOCK) &&
            (handler !== 'handleFormSubmit' || t.getTriggerSourceId() === sheetId);
        });
        var kept = managed[0];
        if (!kept) {
          var builder = ScriptApp.newTrigger(handler);
          kept = handler === 'handleFormSubmit' ? builder.forSpreadsheet(sheetId).onFormSubmit().create() :
                                                  builder.timeBased().everyMinutes(5).create();
          props.setProperty(key, kept.getUniqueId());
        }
        matching.forEach(function(t){if(t.getUniqueId() !== kept.getUniqueId()) ScriptApp.deleteTrigger(t);});
        report.push({handler:handler, triggerId:kept.getUniqueId(), interval:handler === 'handleFormSubmit' ? 'ON_FORM_SUBMIT' : '5_MINUTES'});
      });
      return {triggers:report, automaticPricingChanged:false, emailsSent:false,
              note:'Only triggers owned by the executing account can be inspected or repaired.'};
    } finally { lock.releaseLock(); }
  }
  return {quantities:quantities, validatePricebook:validatePricebook, repairTriggers:repairTriggers};
}());
function capeRoofRepairTriggers20261011() { return CapeRoofRepair20261011.repairTriggers(); }
function capeRoofAuditPricebook20261011() {
  var id = PropertiesService.getScriptProperties().getProperty('GOOGLE_SPREADSHEET_ID');
  if (!id) throw new Error('Existing spreadsheet ID is required.');
  var sheet = SpreadsheetApp.openById(id).getSheetByName('Pricing');
  if (!sheet) throw new Error('Pricing tab not found.');
  return CapeRoofRepair20261011.validatePricebook(sheet.getDataRange().getValues(), new Date());
}

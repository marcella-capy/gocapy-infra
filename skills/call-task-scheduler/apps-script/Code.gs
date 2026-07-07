/**
 * Call Task Scheduler — Google Sheet trigger (Workflow A, full port of create_call_tasks.py).
 *
 * Sheet: https://docs.google.com/spreadsheets/d/1apeJni_cb86f_J5L_Y2UNrd1Xq1nA2iD8exQQBxIM6A
 *
 * How it works: ticking the "Create" checkbox on a row runs the workflow immediately in
 * Google's cloud (no laptop involved): pulls the org's ICP-Yes people from Pipedrive LIVE,
 * creates 3 call activities per person with a phone (D+1 / +5bd / +7bd), posts the no-phone
 * note on the org, pushes no-phone people to the Clay People table and thin orgs (<5 callable)
 * to the Clay Company table, then writes the outcome into Status/Result.
 *
 * ONE-TIME SETUP (after pasting this file into Extensions → Apps Script):
 *   1. Project Settings → Script Properties → add:
 *        PIPEDRIVE_API_TOKEN = <token>          (required)
 *        PIPEDRIVE_DOMAIN    = capy             (optional, defaults to capy)
 *   2. Run the setup() function once (Run → setup). Approve the authorization prompt.
 *      It builds the Requests + Registry tabs, dropdowns, checkboxes, and installs the trigger.
 *
 * Title contract (the daily reconcile routine parses this — keep in sync with the skill):
 *   "Call <n>: <Last Name> from <Organization> for <Principal display name>"
 */

var CLAY_PEOPLE_WEBHOOK = 'https://api.clay.com/v3/sources/webhook/pull-in-data-from-a-webhook-09215211-803a-4bdd-b557-614dd39d381e';
var CLAY_COMPANY_WEBHOOK = 'https://api.clay.com/v3/sources/webhook/pull-in-data-from-a-webhook-b03a3389-0f3b-4405-94e3-ed61dd599cc9';
var ICP_KEY = '1a8684b9333f530c727f9bff307391d3d200c897';      // Person ICP (Yes/No)
var TITLE_KEY = 'ef54f66e8242d193fd263fa16ac83850271b2794';    // Person Job Title
var LINKEDIN_KEY = 'cf2472711fcbe2a22cef32aea82f1a5a555761a8'; // Person LinkedIn Page
var OWNERS = { 'Marcella': 22638704, 'Jonathan': 20845253, 'Sam': 20845572, 'Ericka': 23490137 };

// ICP job-title classifier — mirrors people-icp-classifier (default-Yes; positives win over excludes)
var ICP_POS_PHRASES = ['supply chain manager', 'supply chain', 'product development engineer',
  'program manager', 'category manager', 'contract manager'];
var ICP_POS_WORDS = ['procurement', 'sourcing', 'supplier', 'buyer', 'purchasing', 'buy', 'commodity',
  'forging', 'forged', 'forgings', 'machining', 'machined', 'casting', 'plastic', 'rubber'];
var ICP_NEG_PHRASES = ['human resources', 'm&a', 'e-commerce'];
var ICP_NEG_WORDS = ['janitor', 'custodian', 'babysitter', 'machinist', 'ceo', 'coo', 'chief',
  'marketing', 'hr', 'inventory', 'warehouse', 'payroll', 'sales', 'compliance', 'cloud', 'digital',
  'oracle', 'cnc', 'accounting', 'accountant', 'designer', 'logistics', 'logistic', 'logisti',
  'staff', 'cybersecurity', 'integration', 'finance', 'indirect', 'commercial', 'quality',
  'shipping', 'receiving', 'human', 'welder', 'assembly', 'assembler', 'customer', 'service',
  'mro', 'technology', 'software', 'board', 'qa', 'business', 'account', 'financial', 'talent',
  'acquisition', 'process', 'electronics', 'learning', 'information', 'avionics', 'structures',
  'stress', 'field', 'fleet', 'transportation', 'technician', 'foreman', 'control', 'cost',
  'intern', 'schedule', 'traffic', 'freight', 'workers', 'recruiter', 'capital', 'capex',
  'expeditor', 'test', 'repair', 'flight', 'handler', 'scheduling', 'aftermarket', 'systems',
  'airport', 'scheduler', 'crew', 'electrical', 'substation', 'transformation', 'building',
  'training', 'education', 'investor', 'climate', 'medical', 'culture', 'delivery', 'pmo',
  'data', 'analytics', 'commerce', 'facilities', 'composites', 'engineering', 'maintenance',
  'administrator', 'assistant', 'developer', 'additive', 'raw', 'cyber', 'it', 'chemicals',
  'contractor'];

function classifyTitle(title) {
  var t = String(title || '').trim().toLowerCase();
  if (!t) return '';  // empty title -> leave ICP blank
  var phraseHit = function (ph) {
    return new RegExp('(^|[^a-z0-9])' + ph.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '($|[^a-z0-9])').test(t);
  };
  var tokens = {};
  (t.match(/[a-z0-9]+/g) || []).forEach(function (w) { tokens[w] = true; });
  for (var i = 0; i < ICP_POS_PHRASES.length; i++) if (phraseHit(ICP_POS_PHRASES[i])) return 'Yes';
  for (var j = 0; j < ICP_POS_WORDS.length; j++) if (tokens[ICP_POS_WORDS[j]]) return 'Yes';
  for (var k = 0; k < ICP_NEG_PHRASES.length; k++) if (phraseHit(ICP_NEG_PHRASES[k])) return 'No';
  for (var l = 0; l < ICP_NEG_WORDS.length; l++) if (tokens[ICP_NEG_WORDS[l]]) return 'No';
  return 'Yes';  // default Yes
}
var MIN_CALLABLE = 5;   // orgs with fewer callable people get pushed to the Clay company table
var COL = { PRINCIPAL: 1, ORG: 2, OWNER: 3, TEST: 4, CREATE: 5, STATUS: 6, RESULT: 7 };

// ── one-time setup ────────────────────────────────────────────────────────────────────────────

function setup() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var req = ss.getSheets()[0];
  req.setName('Requests');
  req.getRange(1, 1, 1, 7).setValues([[
    'Principal', 'Organization', 'Owner', 'Test?', 'Create', 'Status', 'Result']])
    .setFontWeight('bold');
  req.setFrozenRows(1);
  req.getRange('D2:E200').insertCheckboxes();
  req.getRange('C2:C200').setDataValidation(
    SpreadsheetApp.newDataValidation().requireValueInList(Object.keys(OWNERS), true).build());

  var reg = ss.getSheetByName('Registry') || ss.insertSheet('Registry');
  reg.getRange(1, 1, 1, 5).setValues([[
    'slug', 'display_name', 'hothawk_workspace_id', 'voicemail_sequence_id', 'sequence_name']])
    .setFontWeight('bold');
  if (reg.getLastRow() < 2) {
    reg.getRange(2, 1, 2, 5).setValues([
      ['franklin', 'Franklin Casting', '0bb515e2-fb32-4676-83ad-ea72e5e909fe',
       '47e2d481-c72f-4712-8067-6c046456e467', 'Franklin Casting (Evergreen)'],
      ['lnp', 'LNP Machining', '04972960-6538-4472-b94e-36d87481b1c5',
       '532ff222-9db3-4344-b292-09eaa35eb62f', 'LNP_Post-Voicemail Sequence_070726'],
    ]);
  }
  req.getRange('A2:A200').setDataValidation(
    SpreadsheetApp.newDataValidation()
      .requireValueInRange(reg.getRange('A2:A50'), true).build());

  // installable trigger (simple onEdit can't call external APIs)
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'handleEdit') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('handleEdit').forSpreadsheet(ss).onEdit().create();
  SpreadsheetApp.getUi().alert('Setup complete. Add PIPEDRIVE_API_TOKEN in Script Properties if you have not yet.');
}

// ── trigger ──────────────────────────────────────────────────────────────────────────────────

function handleEdit(e) {
  var range = e.range, sheet = range.getSheet();
  if (sheet.getName() !== 'Requests') return;
  if (range.getColumn() !== COL.CREATE || range.getRow() < 2) return;
  if (range.getValue() !== true) return;
  var row = range.getRow();
  var statusCell = sheet.getRange(row, COL.STATUS);
  if (statusCell.getValue() !== '') return;  // re-run requires clearing Status first (deliberate)

  statusCell.setValue('processing…');
  SpreadsheetApp.flush();
  try {
    var input = {
      principal: String(sheet.getRange(row, COL.PRINCIPAL).getValue()).trim().toLowerCase(),
      org: String(sheet.getRange(row, COL.ORG).getValue()).trim(),
      owner: String(sheet.getRange(row, COL.OWNER).getValue()).trim(),
      test: sheet.getRange(row, COL.TEST).getValue() === true,
    };
    var summary = runWorkflowA(input);
    statusCell.setValue('ok');
    sheet.getRange(row, COL.RESULT).setValue(summary);
    // if people went to Clay for phone enrichment, re-run this row once in ~12 min to pick
    // up the phones Clay writes back to Pipedrive
    if (!input.test && summary.indexOf('clay: 0p') < 0 && summary.indexOf('clay: ') >= 0) {
      scheduleSecondPass(row, input);
      sheet.getRange(row, COL.RESULT).setValue(summary + ' | 2nd pass in ~12 min');
    }
  } catch (err) {
    statusCell.setValue('error');
    sheet.getRange(row, COL.RESULT).setValue(String(err.message || err));
  }
}

// ── Clay second pass ─────────────────────────────────────────────────────────────────────────
// Clay usually finds phones within ~5 min and writes them back to Pipedrive. When a run pushed
// people to Clay, we schedule a ONE-SHOT re-run of the same row ~12 min later: the live re-read
// picks up the new phones, and the idempotency gate means only newly-phoned people get tasks.

var SECOND_PASS_DELAY_MS = 12 * 60 * 1000;

function scheduleSecondPass(row, input) {
  var props = PropertiesService.getScriptProperties();
  var pending = JSON.parse(props.getProperty('PENDING_SECOND_PASS') || '{}');
  pending[String(row)] = input;
  props.setProperty('PENDING_SECOND_PASS', JSON.stringify(pending));
  ScriptApp.newTrigger('secondPass').timeBased().after(SECOND_PASS_DELAY_MS).create();
}

function secondPass() {
  // remove any fired one-shot triggers for this handler
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'secondPass') ScriptApp.deleteTrigger(t);
  });
  var props = PropertiesService.getScriptProperties();
  var pending = JSON.parse(props.getProperty('PENDING_SECOND_PASS') || '{}');
  props.deleteProperty('PENDING_SECOND_PASS');
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName('Requests');
  for (var row in pending) {
    var input = pending[row];
    input.secondPass = true;
    var resultCell = sheet.getRange(Number(row), COL.RESULT);
    try {
      var summary = runWorkflowA(input);
      resultCell.setValue(resultCell.getValue() + ' | 2nd pass: ' + summary);
    } catch (err) {
      resultCell.setValue(resultCell.getValue() + ' | 2nd pass error: ' + String(err.message || err));
    }
  }
}

// ── workflow A ───────────────────────────────────────────────────────────────────────────────

function runWorkflowA(input) {
  if (!input.principal) throw new Error('Principal is empty');
  if (!input.org) throw new Error('Organization is empty');
  var ownerId = OWNERS[input.owner];
  if (!ownerId) throw new Error('Unknown owner "' + input.owner + '" — use ' + Object.keys(OWNERS).join('/'));
  var registry = readRegistry();
  var reg = registry[input.principal];
  if (!reg) throw new Error('Principal "' + input.principal + '" not in the Registry tab');

  var org = resolveOrg(input.org);
  var persons = orgPersons(org.id);

  // classify blank-ICP people from their job title and write the verdict back to Pipedrive
  var classified = 0;
  persons.forEach(function (p) {
    if (String(p[ICP_KEY] || '').trim() !== '') return;
    var verdict = classifyTitle(p[TITLE_KEY]);
    if (!verdict) return;  // empty title stays blank
    var body = {};
    body[ICP_KEY] = verdict;
    pd('PUT', '/api/v1/persons/' + p.id, null, body);
    p[ICP_KEY] = verdict;
    classified++;
  });

  var icpYes = persons.filter(function (p) {
    return String(p[ICP_KEY] || '').trim().toLowerCase() === 'yes';
  });
  var withPhone = icpYes.filter(function (p) { return phones(p).length > 0; });
  var noPhone = icpYes.filter(function (p) { return phones(p).length === 0; });

  // idempotency: skip people who already have an open call task from this skill
  var already = openSkillPersonIds(registry);
  var todo = withPhone.filter(function (p) { return !already[String(p.id)]; });
  var skipped = withPhone.length - todo.length;
  if (input.test) {
    if (skipped > 0) return 'TEST skipped: ' + skipped + ' person(s) already have open call tasks';
    todo = todo.slice(0, 1);
  }

  var dueDates = [1, 5, 7].map(function (n) { return addBusinessDays(new Date(), n); });
  var seqCount = input.test ? 1 : 3;
  var createdCount = 0;
  todo.forEach(function (p) {
    var note = 'Call ' + p.name + ' - ' + (p[TITLE_KEY] || 'no title') + ' @ ' + phones(p).join(', ');
    for (var n = 1; n <= seqCount; n++) {
      var body = {
        subject: 'Call ' + n + ': ' + lastName(p) + ' from ' + org.name + ' for ' + reg.display_name,
        type: 'call',
        owner_id: ownerId,
        org_id: org.id,
        due_date: isoDate(dueDates[n - 1]),
        participants: [{ person_id: Number(p.id), primary: true }],
        note: n === 1
          ? note + '<br><br>[Clicking "Mark As Done" moves lead to a Voicemail Email Sequence in HotHawk]'
          : note,
      };
      pd('POST', '/api/v2/activities', null, body);
      createdCount++;
    }
  });

  var noteMade = 0, clayPeople = 0, clayCompany = 0;
  if (!input.test && !input.secondPass) {  // second pass never re-pushes to Clay
    if (noPhone.length) noteMade = postNoPhoneNote(org, noPhone, reg.display_name, input.owner);
    noPhone.forEach(function (p) {
      var email = primaryEmail(p);
      var ok = clayPost(CLAY_PEOPLE_WEBHOOK, {
        name: p.name, first_name: firstName(p), last_name: lastName(p),
        job_title: p[TITLE_KEY] || '', email: email,
        linkedin_url: p[LINKEDIN_KEY] || '',
        company_name: org.name, company_domain: email.indexOf('@') > 0 ? email.split('@')[1] : '',
        pipedrive_person_id: Number(p.id), pipedrive_org_id: org.id,
      });
      if (ok) clayPeople++;
    });
    if (withPhone.length < MIN_CALLABLE) {
      if (clayPost(CLAY_COMPANY_WEBHOOK, {
        company_name: org.name, company_domain: org.domain, pipedrive_org_id: org.id,
        icp_yes_count: icpYes.length, with_phone_count: withPhone.length, no_phone_count: noPhone.length,
      })) clayCompany++;
    }
  }

  return (input.test ? 'TEST: ' : '') + todo.length + ' people, ' + createdCount + ' tasks, '
    + skipped + ' skipped, ' + noPhone.length + ' no-phone'
    + (classified ? ', ' + classified + ' ICP-classified' : '')
    + (noteMade ? ' (note posted)' : '')
    + (clayPeople || clayCompany ? ', clay: ' + clayPeople + 'p/' + clayCompany + 'c' : '');
}

// ── pipedrive helpers ────────────────────────────────────────────────────────────────────────

function pd(method, path, params, body) {
  var props = PropertiesService.getScriptProperties();
  var token = props.getProperty('PIPEDRIVE_API_TOKEN');
  if (!token) throw new Error('PIPEDRIVE_API_TOKEN missing in Script Properties');
  var domain = props.getProperty('PIPEDRIVE_DOMAIN') || 'capy';
  var qs = 'api_token=' + encodeURIComponent(token);
  for (var k in (params || {})) {
    if (params[k] !== null && params[k] !== undefined) qs += '&' + k + '=' + encodeURIComponent(params[k]);
  }
  var resp = UrlFetchApp.fetch('https://' + domain + '.pipedrive.com' + path + '?' + qs, {
    method: method.toLowerCase(),
    contentType: 'application/json',
    payload: body ? JSON.stringify(body) : undefined,
    muteHttpExceptions: true,
  });
  var code = resp.getResponseCode();
  if (code >= 300) throw new Error('Pipedrive ' + method + ' ' + path + ' -> HTTP ' + code + ': '
    + resp.getContentText().slice(0, 300));
  return JSON.parse(resp.getContentText() || '{}');
}

function resolveOrg(nameOrId) {
  if (/^\d+$/.test(nameOrId)) {
    var d = pd('GET', '/api/v1/organizations/' + nameOrId).data;
    if (!d) throw new Error('Org id ' + nameOrId + ' not found');
    return { id: Number(d.id), name: d.name, domain: cleanDomain(d.website) };
  }
  var items = (pd('GET', '/api/v2/organizations/search', { term: nameOrId, limit: 20 }).data || {}).items || [];
  var hits = items.map(function (i) { return i.item; });
  var exact = hits.filter(function (h) { return h.name.toLowerCase() === nameOrId.toLowerCase(); });
  if (exact.length === 1) hits = exact;
  if (hits.length === 0) throw new Error('No Pipedrive org matches "' + nameOrId + '"');
  if (hits.length > 1) {
    throw new Error('"' + nameOrId + '" is ambiguous: '
      + hits.slice(0, 6).map(function (h) { return h.id + '=' + h.name; }).join('; ')
      + ' — put the org ID in the Organization cell');
  }
  var full = pd('GET', '/api/v1/organizations/' + hits[0].id).data || {};
  return { id: Number(hits[0].id), name: hits[0].name, domain: cleanDomain(full.website) };
}

function orgPersons(orgId) {
  var out = [], start = 0;
  while (true) {
    var r = pd('GET', '/api/v1/organizations/' + orgId + '/persons', { start: start, limit: 500 });
    out = out.concat(r.data || []);
    var more = r.additional_data && r.additional_data.pagination
      && r.additional_data.pagination.more_items_in_collection;
    if (!more) break;
    start += 500;
  }
  return out;
}

function openSkillPersonIds(registry) {
  var displays = {};
  for (var slug in registry) displays[registry[slug].display_name.toLowerCase()] = true;
  var ids = {}, cursor = null;
  while (true) {
    var params = { done: 'false', limit: 500 };
    if (cursor) params.cursor = cursor;
    var r = pd('GET', '/api/v2/activities', params);
    (r.data || []).forEach(function (act) {
      if (act.type !== 'call') return;
      var m = /^Call [123]: .+ from .+ for (.+)$/.exec(act.subject || '');
      if (!m || !displays[m[1].trim().toLowerCase()]) return;
      var pid = activityPersonId(act);
      if (pid) ids[String(pid)] = true;
    });
    cursor = r.additional_data && r.additional_data.next_cursor;
    if (!cursor) break;
  }
  return ids;
}

function activityPersonId(act) {
  var parts = act.participants || [];
  for (var i = 0; i < parts.length; i++) if (parts[i].primary && parts[i].person_id) return parts[i].person_id;
  for (var j = 0; j < parts.length; j++) if (parts[j].person_id) return parts[j].person_id;
  return act.person_id || null;
}

function postNoPhoneNote(org, noPhone, display, ownerName) {
  var header = '<b>Needs phone numbers for ' + display + ' call sequence — @' + ownerName + '</b>';
  var existing = pd('GET', '/api/v1/notes', { org_id: org.id, limit: 100 }).data || [];
  for (var i = 0; i < existing.length; i++) {
    if ((existing[i].content || '').indexOf(header) >= 0) return 0;  // dedupe
  }
  var rows = noPhone.map(function (p) {
    return '<li>' + p.name + ' — ' + (p[TITLE_KEY] || 'no title') + '</li>';
  }).join('');
  pd('POST', '/api/v1/notes', null, { content: header + '<ul>' + rows + '</ul>', org_id: org.id });
  return 1;
}

// ── small utils ──────────────────────────────────────────────────────────────────────────────

function readRegistry() {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName('Registry');
  if (!sheet) throw new Error('Registry tab missing — run setup()');
  var out = {};
  sheet.getDataRange().getValues().slice(1).forEach(function (r) {
    if (r[0]) out[String(r[0]).trim().toLowerCase()] = {
      display_name: String(r[1]).trim(), workspace_id: String(r[2]).trim(),
      sequence_id: String(r[3]).trim(), sequence_name: String(r[4]).trim(),
    };
  });
  return out;
}

function clayPost(url, payload) {
  var resp = UrlFetchApp.fetch(url, {
    method: 'post', contentType: 'application/json',
    payload: JSON.stringify(payload), muteHttpExceptions: true,
  });
  return resp.getResponseCode() < 300;
}

function phones(p) {
  return (p.phone || []).map(function (ph) { return (ph.value || '').trim(); })
    .filter(function (v) { return v; });
}

function primaryEmail(p) {
  var e = (p.email || []).map(function (x) { return (x.value || '').trim(); })
    .filter(function (v) { return v; });
  return e.length ? e[0] : '';
}

function firstName(p) {
  if (p.first_name) return String(p.first_name).trim();
  var parts = String(p.name || '').trim().split(/\s+/);
  return parts.length > 1 ? parts.slice(0, -1).join(' ') : (parts[0] || '');
}

function lastName(p) {
  if (p.last_name) return String(p.last_name).trim();
  var parts = String(p.name || '').trim().split(/\s+/);
  return parts.length ? parts[parts.length - 1] : '?';
}

function addBusinessDays(d, n) {
  var out = new Date(d);
  while (n > 0) {
    out.setDate(out.getDate() + 1);
    if (out.getDay() !== 0 && out.getDay() !== 6) n--;
  }
  return out;
}

function isoDate(d) {
  return Utilities.formatDate(d, Session.getScriptTimeZone(), 'yyyy-MM-dd');
}

function cleanDomain(w) {
  return String(w || '').replace(/^https?:\/\//, '').replace(/^www\./, '').replace(/\/+$/, '');
}

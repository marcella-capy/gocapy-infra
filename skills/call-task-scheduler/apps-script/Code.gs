/**
 * Call Task Scheduler — Google Sheet trigger (Workflow A, full port of create_call_tasks.py).
 *
 * Sheet: https://docs.google.com/spreadsheets/d/1apeJni_cb86f_J5L_Y2UNrd1Xq1nA2iD8exQQBxIM6A
 *
 * How it works: ticking the "Create" checkbox on a row runs the workflow immediately in
 * Google's cloud (no laptop involved): pulls the org's ICP-Yes people from Pipedrive LIVE,
 * drops out-of-territory people (per-principal rules), ranks by job-title tier (mgmt-level
 * sourcing/procurement titles first), caps at 25 people per run (re-run rotates to the next 25;
 * a person is re-eligible 45 days after their last call task), creates 3 call activities per
 * person (Call 1s paced 5 per business day; Call 2/3 at +5/+7bd from each person's Call 1),
 * posts the no-phone note on the org, pushes up to 10 tier-1-title no-phone people to the Clay
 * People table and thin orgs (<5 callable) to the Clay Company table, then writes the outcome
 * into Status/Result.
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
var ORG_EMAIL_PATTERN_KEY = '3ceb3b7c740bde695671e7cf393cb520e2fa7a65'; // Org Email Pattern
var OWNERS = { 'Marcella': 22638704, 'Jonathan': 20845253, 'Sam': 20845572, 'Ericka': 23490137, 'Mark': 25200747 };

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
var MAX_PEOPLE_PER_RUN = 25;      // hard cap per run; re-run within ROTATION_DAYS gets the NEXT 25
var ROTATION_DAYS = 45;           // a person with any call task in this window is not re-tasked
var CALLS_PER_DAY = 5;            // Call-1s per company per business day
var TIER3_MAX_WITHPHONE = 100;    // >100 phoned people -> only tier 1/2 titles get tasks
var CLAY_PEOPLE_MAX = 10;         // phone-finder table: tier-1 titles only, max 10 per run
var COL = { PRINCIPAL: 1, ORG: 2, OWNER: 3, TEST: 4, CREATE: 5, STATUS: 6, RESULT: 7 };

var PERSON_STATE_KEY = '67e678e89a3a8eb69a576f550d6446b224f17980';   // Person Contact State
var PERSON_CITY_KEY = '6ddd2290f7538daf39859ad788295374aa1ae5f9';    // Person Contact City
var PERSON_COUNTRY_KEY = 'cec25dc64c9744b2f1c5109572ef6285eef4cf27'; // Person Contact Country
var ORG_STATE_KEY = '997336778f6e562b0d0db2578037c30125e72f85';      // Company State (fallback)

// ── territory rules (mirror of go-capy-outreach/shared-references/client-territories.json) ────
// Keyed by principal display name (lowercase). Principals absent -> no restriction.
// Only a CONFIRMED out-of-territory location drops a person, except strict_unknown clients.
var TERRITORIES = {
  'patriot forge': { include_countries: ['United States'], include_states: ['WA', 'OR', 'CA', 'AZ', 'NV'], exclude_countries: ['Canada'] },
  'tech-max': { exclude_states: ['FL', 'MA', 'IL', 'PA', 'VT'] },
  'general foundry': { exclude_states: ['NV', 'UT', 'CO', 'MA', 'CT'] },
  'harvey vogel': { include_states: ['CA'], socal_only: true, strict_unknown: true },
  'megatech': { include_countries: ['United States'], exclude_countries: ['Canada'] },
};

var STATE_ABBR = { 'alabama': 'AL', 'alaska': 'AK', 'arizona': 'AZ', 'arkansas': 'AR',
  'california': 'CA', 'colorado': 'CO', 'connecticut': 'CT', 'delaware': 'DE', 'florida': 'FL',
  'georgia': 'GA', 'hawaii': 'HI', 'idaho': 'ID', 'illinois': 'IL', 'indiana': 'IN', 'iowa': 'IA',
  'kansas': 'KS', 'kentucky': 'KY', 'louisiana': 'LA', 'maine': 'ME', 'maryland': 'MD',
  'massachusetts': 'MA', 'michigan': 'MI', 'minnesota': 'MN', 'mississippi': 'MS',
  'missouri': 'MO', 'montana': 'MT', 'nebraska': 'NE', 'nevada': 'NV', 'new hampshire': 'NH',
  'new jersey': 'NJ', 'new mexico': 'NM', 'new york': 'NY', 'north carolina': 'NC',
  'north dakota': 'ND', 'ohio': 'OH', 'oklahoma': 'OK', 'oregon': 'OR', 'pennsylvania': 'PA',
  'rhode island': 'RI', 'south carolina': 'SC', 'south dakota': 'SD', 'tennessee': 'TN',
  'texas': 'TX', 'utah': 'UT', 'vermont': 'VT', 'virginia': 'VA', 'washington': 'WA',
  'west virginia': 'WV', 'wisconsin': 'WI', 'wyoming': 'WY', 'district of columbia': 'DC',
  'washington dc': 'DC', 'puerto rico': 'PR' };
var US_ABBRS = {};
for (var _sk in STATE_ABBR) US_ABBRS[STATE_ABBR[_sk]] = true;
var CA_PROVINCES = { 'ontario': 1, 'on': 1, 'quebec': 1, 'québec': 1, 'qc': 1,
  'british columbia': 1, 'bc': 1, 'alberta': 1, 'ab': 1, 'manitoba': 1, 'mb': 1,
  'saskatchewan': 1, 'sk': 1, 'nova scotia': 1, 'ns': 1, 'new brunswick': 1, 'nb': 1,
  'newfoundland and labrador': 1, 'nl': 1, 'prince edward island': 1, 'pe': 1, 'pei': 1,
  'northwest territories': 1, 'nt': 1, 'yukon': 1, 'yt': 1, 'nunavut': 1, 'nu': 1 };
var COUNTRY_ALIASES = { 'us': 'United States', 'u.s.': 'United States', 'u.s.a.': 'United States',
  'usa': 'United States', 'united states of america': 'United States',
  'united states': 'United States', 'america': 'United States', 'canada': 'Canada',
  'mexico': 'Mexico', 'méxico': 'Mexico' };
var SOCAL_CITIES = ['los angeles', 'long beach', 'glendale', 'santa clarita', 'lancaster',
  'palmdale', 'pomona', 'torrance', 'pasadena', 'el monte', 'downey', 'inglewood', 'west covina',
  'norwalk', 'burbank', 'compton', 'carson', 'santa monica', 'hawthorne', 'whittier', 'alhambra',
  'lakewood', 'bellflower', 'baldwin park', 'lynwood', 'redondo beach', 'pico rivera',
  'montebello', 'monterey park', 'gardena', 'huntington park', 'arcadia', 'diamond bar',
  'paramount', 'rosemead', 'cerritos', 'covina', 'azusa', 'glendora', 'culver city',
  'san gabriel', 'rancho palos verdes', 'la mirada', 'el segundo', 'manhattan beach',
  'beverly hills', 'calabasas', 'west hollywood', 'santa fe springs', 'industry', 'commerce',
  'vernon', 'agoura hills', 'claremont', 'san dimas', 'walnut', 'temple city', 'monrovia',
  'duarte', 'south pasadena', 'hermosa beach', 'sierra madre', 'malibu', 'lawndale', 'bell',
  'maywood', 'cudahy', 'san fernando', 'la verne', 'signal hill', 'hawaiian gardens', 'artesia',
  'westlake village', 'lomita', 'la puente', 'la canada flintridge', 'la cañada flintridge',
  'south gate', 'irwindale', 'avalon', 'anaheim', 'santa ana', 'irvine', 'huntington beach',
  'garden grove', 'orange', 'fullerton', 'costa mesa', 'mission viejo', 'westminster',
  'newport beach', 'buena park', 'lake forest', 'tustin', 'yorba linda', 'san clemente',
  'laguna niguel', 'la habra', 'fountain valley', 'anaheim hills', 'placentia',
  'rancho santa margarita', 'aliso viejo', 'cypress', 'brea', 'stanton', 'dana point',
  'laguna hills', 'san juan capistrano', 'seal beach', 'laguna beach', 'la palma',
  'los alamitos', 'villa park', 'san diego', 'chula vista', 'oceanside', 'escondido',
  'carlsbad', 'el cajon', 'vista', 'san marcos', 'encinitas', 'national city', 'la mesa',
  'santee', 'poway', 'coronado', 'imperial beach', 'lemon grove', 'solana beach', 'del mar',
  'riverside', 'moreno valley', 'corona', 'temecula', 'murrieta', 'jurupa valley', 'menifee',
  'hemet', 'indio', 'perris', 'eastvale', 'cathedral city', 'palm desert', 'lake elsinore',
  'palm springs', 'coachella', 'beaumont', 'san jacinto', 'wildomar', 'la quinta', 'banning',
  'norco', 'desert hot springs', 'rancho mirage', 'canyon lake', 'calimesa', 'blythe',
  'san bernardino', 'fontana', 'rancho cucamonga', 'ontario', 'victorville', 'rialto',
  'hesperia', 'chino', 'chino hills', 'upland', 'apple valley', 'redlands', 'highland',
  'colton', 'yucaipa', 'montclair', 'adelanto', 'twentynine palms', 'loma linda', 'barstow',
  'grand terrace', 'big bear lake', 'needles', 'oxnard', 'thousand oaks', 'simi valley',
  'ventura', 'san buenaventura', 'camarillo', 'moorpark', 'santa paula', 'port hueneme',
  'fillmore', 'ojai', 'el centro', 'calexico', 'brawley', 'imperial', 'holtville',
  'westmorland', 'calipatria'];
var SOCAL_SET = {};
SOCAL_CITIES.forEach(function (c) { SOCAL_SET[c] = true; });

function normState(raw) {
  var s = String(raw || '').trim();
  if (!s) return { abbr: '', isUS: false, isCA: false };
  var low = s.toLowerCase();
  if (CA_PROVINCES[low]) return { abbr: '', isUS: false, isCA: true };
  if (s.length === 2 && US_ABBRS[s.toUpperCase()]) return { abbr: s.toUpperCase(), isUS: true, isCA: false };
  if (STATE_ABBR[low]) return { abbr: STATE_ABBR[low], isUS: true, isCA: false };
  return { abbr: '', isUS: false, isCA: false };
}

function normCountry(raw) {
  var s = String(raw || '').trim();
  if (!s) return '';
  return COUNTRY_ALIASES[s.toLowerCase()] || s;
}

// Port of territory_filter.py keep(): true = person may be called for this principal.
function keepInTerritory(rule, person, orgState) {
  if (!rule) return true;
  var st = normState(person[PERSON_STATE_KEY] || orgState);
  var country = normCountry(person[PERSON_COUNTRY_KEY]);
  if (!country) { if (st.isUS) country = 'United States'; else if (st.isCA) country = 'Canada'; }
  var city = String(person[PERSON_CITY_KEY] || '').trim().toLowerCase();
  var strict = !!rule.strict_unknown;
  var i;
  if (rule.exclude_states && st.abbr) {
    for (i = 0; i < rule.exclude_states.length; i++) if (rule.exclude_states[i] === st.abbr) return false;
  }
  if (rule.exclude_countries && country) {
    for (i = 0; i < rule.exclude_countries.length; i++) if (normCountry(rule.exclude_countries[i]) === country) return false;
  }
  if (rule.include_states) {
    if (st.abbr) {
      if (rule.include_states.indexOf(st.abbr) < 0) return false;
    } else if (country && country !== 'United States') return false;
    else if (strict) return false;
  }
  if (rule.include_countries) {
    if (country) {
      if (rule.include_countries.map(normCountry).indexOf(country) < 0) return false;
    } else if (strict) return false;
  }
  if (rule.socal_only) {
    if (st.abbr && st.abbr !== 'CA') return false;
    if (city) { if (!SOCAL_SET[city]) return false; }
    else if (strict) return false;
  }
  return true;
}

// ── title prioritization (rules confirmed by Marcella 2026-07-08) ─────────────────────────────
// Tier 1: functional keyword + manager/sr/senior/director, minus program managers, engineers,
//         buyers, specialists, entry-level, VP, C-level.
// Tier 2: VP-level with the same functional keywords (used only when tier 1 < 25).
// Tier 3: everyone else (buyers land here) — skipped entirely when org has >100 phoned people.
var FUNC_RE = /(sourcing|commodit|purchasing|supplier|procurement|supply\s*chain|category)/;
var SENIOR_RE = /(manager|mgr\.?|sr\.?(\s|$)|senior|director)/;
var VP_RE = /(vp|v\.p\.|vice\s*president)/;
var TIER1_EXCLUDE_RE = /(program\s*manager|engineer|buyer|specialist|associate|junior|jr\.?(\s|$)|coordinator|analyst|intern(\s|$)|assistant|chief|c[pes]o(\s|$)|president)/;

function tierOf(p) {
  var t = String(p[TITLE_KEY] || '').toLowerCase();
  if (!t) return 3;
  if (FUNC_RE.test(t)) {
    if (VP_RE.test(t) && !TIER1_EXCLUDE_RE.test(t.replace(VP_RE, ''))) return 2;
    if (SENIOR_RE.test(t) && !VP_RE.test(t) && !TIER1_EXCLUDE_RE.test(t)) return 1;
  }
  return 3;
}

// Order people tier1 -> (tier2 if tier1 phoned < 25) -> tier3 (only when org is not huge),
// cap at MAX_PEOPLE_PER_RUN. Returns { list, deprioritized }.
function prioritize(people, totalWithPhone) {
  var t1 = [], t2 = [], t3 = [];
  people.forEach(function (p) {
    var t = tierOf(p);
    (t === 1 ? t1 : t === 2 ? t2 : t3).push(p);
  });
  var ordered = t1.slice();
  if (t1.length < 25) ordered = ordered.concat(t2);
  if (totalWithPhone <= TIER3_MAX_WITHPHONE) ordered = ordered.concat(t3);
  var list = ordered.slice(0, MAX_PEOPLE_PER_RUN);
  return { list: list, deprioritized: people.length - list.length };
}

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

  // territory gate: out-of-territory people are skipped ENTIRELY (no tasks, no Clay)
  var territoryRule = TERRITORIES[reg.display_name.toLowerCase()];
  var outOfTerritory = 0;
  if (territoryRule) {
    var orgState = String((pd('GET', '/api/v1/organizations/' + org.id).data || {})[ORG_STATE_KEY] || '');
    icpYes = icpYes.filter(function (p) {
      if (keepInTerritory(territoryRule, p, orgState)) return true;
      outOfTerritory++;
      return false;
    });
  }

  // rotation source: person_ids with any call task from this skill that is still open OR was
  // completed in the last ROTATION_DAYS — they are skipped, so re-runs get the NEXT 25 people
  var already = recentSkillPersonIds(registry);

  // duplicate-record guard: same human twice at one org (same normalized name, since duplicate
  // records rarely share an email) -> keep ONE record per human. Preference: a record that
  // already has open tasks (so re-runs stay idempotent) > has email > more phones > older id.
  // The losing duplicates are listed on the org note so they can be merged later.
  var byHuman = {}, dupPairs = [];
  icpYes.forEach(function (p) {
    // last name + first initial, so "Reggie West" and "Reginald West" collide too
    var key = lastName(p).toLowerCase().replace(/[^a-z]/g, '') + '|'
      + firstName(p).slice(0, 1).toLowerCase();
    var cur = byHuman[key];
    if (!cur) { byHuman[key] = p; return; }
    var score = function (x) {
      return [(already[String(x.id)] ? 1 : 0), (primaryEmail(x) ? 1 : 0),
              phones(x).length, -Number(x.id)];
    };
    var a = score(p), b = score(cur), better = 0;
    for (var s = 0; s < a.length && !better; s++) better = a[s] - b[s];
    if (better > 0) { dupPairs.push({ kept: p, ignored: cur }); byHuman[key] = p; }
    else dupPairs.push({ kept: cur, ignored: p });
  });
  var dedupedOut = dupPairs.length;
  icpYes = Object.keys(byHuman).map(function (k) { return byHuman[k]; });

  var withPhone = icpYes.filter(function (p) { return phones(p).length > 0; });
  var noPhone = icpYes.filter(function (p) { return phones(p).length === 0; });

  var fresh = withPhone.filter(function (p) { return !already[String(p.id)]; });
  var skipped = withPhone.length - fresh.length;

  // priority order (tier 1 mgmt titles first) + hard cap per run
  var pri = prioritize(fresh, withPhone.length);
  var todo = pri.list;
  var deprioritized = pri.deprioritized;

  if (input.test) {
    if (skipped > 0) return 'org ' + org.id + ' (' + org.name + '): TEST skipped: ' + skipped
      + ' person(s) already have recent call tasks';
    todo = todo.slice(0, 1);
  }

  // pacing: CALLS_PER_DAY Call-1s per business day, in priority order; each person's
  // Call 2/3 shift with their own Call 1
  var seqCount = input.test ? 1 : 3;
  var createdCount = 0, offsets = [1, 5, 7];
  todo.forEach(function (p, idx) {
    var batch = Math.floor(idx / CALLS_PER_DAY);
    var note = 'Call ' + p.name + ' - ' + (p[TITLE_KEY] || 'no title') + ' @ ' + phones(p).join(', ');
    for (var n = 1; n <= seqCount; n++) {
      var body = {
        subject: 'Call ' + n + ': ' + lastName(p) + ' from ' + org.name + ' for ' + reg.display_name,
        type: 'call',
        owner_id: ownerId,
        org_id: org.id,
        due_date: isoDate(addBusinessDays(new Date(), offsets[n - 1] + batch)),
        participants: [{ person_id: Number(p.id), primary: true }],
        note: n === 1
          ? note + '<br><br>[Clicking "Mark As Done" moves lead to a Voicemail Email Sequence in HotHawk]'
          : note,
      };
      pd('POST', '/api/v2/activities', null, body);
      createdCount++;
    }
  });
  var call1Days = Math.ceil(todo.length / CALLS_PER_DAY);

  var noteMade = 0, clayPeople = 0, clayCompany = 0;
  // phone-finder table: only no-phone people with TIER-1 titles, max CLAY_PEOPLE_MAX per run
  var clayCandidates = noPhone.filter(function (p) { return tierOf(p) === 1; })
    .slice(0, CLAY_PEOPLE_MAX);
  if (!input.test && !input.secondPass) {  // second pass never re-pushes to Clay
    if (noPhone.length) noteMade = postNoPhoneNote(org, noPhone, reg.display_name, input.owner);
    if (dupPairs.length) postDuplicatesNote(org, dupPairs, input.owner);
    clayCandidates.forEach(function (p) {
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
        email_pattern: org.email_pattern || '',
        icp_yes_count: icpYes.length, with_phone_count: withPhone.length, no_phone_count: noPhone.length,
      })) clayCompany++;
    }
  }

  return 'org ' + org.id + ' (' + org.name + '): ' + (input.test ? 'TEST: ' : '')
    + todo.length + ' people, ' + createdCount + ' tasks'
    + (call1Days > 1 ? ' (Call 1s over ' + call1Days + ' days)' : '') + ', '
    + skipped + ' skipped, ' + noPhone.length + ' no-phone'
    + (outOfTerritory ? ', ' + outOfTerritory + ' out-of-territory' : '')
    + (deprioritized ? ', ' + deprioritized + ' deprioritized (cap ' + MAX_PEOPLE_PER_RUN + ')' : '')
    + (classified ? ', ' + classified + ' ICP-classified' : '')
    + (dedupedOut ? ', ' + dedupedOut + ' duplicate record(s) ignored' : '')
    + (noteMade ? ' (note posted)' : '')
    + (clayPeople || clayCompany
        ? ', clay: ' + clayPeople + 'p' + (noPhone.length > clayPeople ? ' (of ' + noPhone.length + ' no-phone)' : '')
          + '/' + clayCompany + 'c'
        : '');
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
    return { id: Number(d.id), name: d.name, domain: cleanDomain(d.website),
             email_pattern: String(d[ORG_EMAIL_PATTERN_KEY] || '') };
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
  return { id: Number(hits[0].id), name: hits[0].name, domain: cleanDomain(full.website),
           email_pattern: String(full[ORG_EMAIL_PATTERN_KEY] || '') };
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

// People with a skill call task that is still OPEN, or was COMPLETED within ROTATION_DAYS —
// both are excluded from new runs, so a re-run rotates to the next batch of people.
function recentSkillPersonIds(registry) {
  var displays = {};
  for (var slug in registry) displays[registry[slug].display_name.toLowerCase()] = true;
  var ids = {};
  var since = new Date();
  since.setDate(since.getDate() - ROTATION_DAYS);
  var sinceIso = Utilities.formatDate(since, 'UTC', "yyyy-MM-dd'T'HH:mm:ss'Z'");

  [{ done: 'false' }, { done: 'true', updated_since: sinceIso }].forEach(function (extra) {
    var cursor = null;
    while (true) {
      var params = { limit: 500 };
      for (var k in extra) params[k] = extra[k];
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
  });
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

function postDuplicatesNote(org, dupPairs, ownerName) {
  var header = '<b>Duplicate person records to merge — @' + ownerName + '</b>';
  var existing = pd('GET', '/api/v1/notes', { org_id: org.id, limit: 100 }).data || [];
  for (var i = 0; i < existing.length; i++) {
    if ((existing[i].content || '').indexOf(header) >= 0) return;  // dedupe
  }
  var rows = dupPairs.map(function (d) {
    return '<li>' + d.ignored.name + ' (id ' + d.ignored.id + ') looks like a duplicate of '
      + d.kept.name + ' (id ' + d.kept.id + ') — call tasks were created on id '
      + d.kept.id + ' only</li>';
  }).join('');
  pd('POST', '/api/v1/notes', null, { content: header + '<ul>' + rows + '</ul>', org_id: org.id });
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

---
name: org-quick-add
description: >
  Interactive one-shot pipeline: the user names a company (or pastes a funding-news snippet)
  and Kodie identifies it, dedupes against Pipedrive, creates or updates the organization with
  every fillable field, probes the mail domain for catch-all, fills the company LinkedIn page,
  writes a full Company Research markdown, and appends the G:\ org index. Triggers on
  "add <company> to pipedrive", "enrich and add <company>", "same for <company>", "org quick add",
  or a pasted news snippet like "X, a <city>-based <what> company, raised $NM led by <investor>".
  Batch-capable — one or many companies per message, one summary table at the end. Defaults:
  fully autonomous identification (ask only when genuinely ambiguous), Org ICP=Yes and
  Find People=Yes on every org (overridable in the message, e.g. "no icp" / "no find people").
  Existing orgs are updated IN PLACE: fill blanks only, flip ICP/Find People if unset, append
  fresh news to existing research — never overwrite a non-empty field. Distinct from
  org-research-agent-v2 (ClickUp-task/filter-driven, credit-tiered); this skill is for ad-hoc,
  human-initiated single-org adds.
---

# org-quick-add — name a company, get a fully-enriched Pipedrive org

## Context

Codifies the pipeline run interactively on 2026-07-23 for Atoms (69609), Crystalys (69610),
Gritt (69611), Inner Logic (69612), RVAG (69613), Hilo (69614), deltaVision (69615),
Bluecore (69616), plus the update-in-place path for the pre-existing Sila (57043).
Per company: identify → dedupe → research → create/update → catch-all → LinkedIn →
Company Research → G:\ index → report.

## Prereqs

- `python3.14` on PATH; credentials auto-load from `~/.claude/global.env` via `capy_env`
  (PIPEDRIVE_*, PERPLEXITY_API_KEY, FIRECRAWL_API_KEY — no manual env setup).
- All commands run from the outreach repo root:
  `C:\Users\marce\.claude\plugins\marketplaces\gocapy-claude-plugin\go-capy-outreach`
  (referred to below as `{OUTREACH}`). Scripts reused from there — this skill ships no code:
  - `skills/pipedrive/scripts/pipedrive_call.py` — generic Pipedrive REST
  - `scripts/catchall_probe.py` — SMTP catch-all probe
  - `skills/org-research-agent-v2/scripts/linkedin_fill.py` — LinkedIn waterfall
    (Perplexity → site scrape/slug-verify → Clay async)
- Stage long markdown to the session **scratchpad dir (absolute Windows path)** — never `/tmp`
  (doesn't exist on Windows; a heredoc reading it tracebacks).

## Field keys (org)

| Field | Key | Write as |
|---|---|---|
| Website / Address / Employees / LinkedIn | `website` / `address` / `employee_count` / `linkedin` | built-in, top level |
| Org Industry | `6c03ee8ae1fd876379a086dfbafc911148124f25` | varchar — reuse existing taxonomy values (e.g. "Nuclear & Energy", "Robotics & Industrial Automation", "Medical Device - Diagnostics & Therapeutic Devices", "Aerospace & Defense - Precision Manufacturing") |
| Org ICP | `d8a4cf212fe697b6aa8033615631deacbe0b571e` | varchar `"Yes"` |
| Find People | `487087662337240aabd62722e8828f8811d6b17e` | NUMBER `196` (Yes) — a string 400s |
| Company State | `997336778f6e562b0d0db2578037c30125e72f85` | varchar — leave blank when nothing maps (Swiss cantons etc.) |
| Company City | `593adc3bc1f56d0bb9fba9852e08bee9a08e351a` | varchar |
| Org Email Catch-All | `0244bd942390dee51e95158225e81af9b2438de4` | LIST of option id — `[194]` yes / `[195]` no / `[193]` unknown |
| Company Description | `f7fbc5d7c7622e9d6b42dcc1b12c25fa91e59725` | text, 2–4 factual sentences incl. the funding line |
| Company Research | `70c38286d0d6726717df98641a510c789a813afd` | text, markdown (Step 6) |

Gotchas (both hit live 2026-07-23):
- **Never put a query string in the path** — `pipedrive_call.py GET "/organizations/search?term=x"`
  returns **401** (the `?` breaks token appending). Always use repeatable `--query k=v`.
- The "Organization Specific Industry" key `3a72f18a9a62a646bfe510f477741f5fd34611ab` is **dead**
  (field deleted; POST 400s `ERR_SCHEMA_VALIDATION_FAILED`). Don't write it; if
  `shared-references/pipedrive-field-keys.md` still lists it, ignore that row.

## Workflow (loop per company; batches share one final report)

### Step 1 — Dedupe against Pipedrive
```
python3.14 skills/pipedrive/scripts/pipedrive_call.py GET /organizations/search \
    --query "term=<name>" --query "limit=5"
```
Match found → jump to **Step 7** (update in place). Watch name variants — "Sila" matched
"Sila Nanotechnologies, Inc."; also try the domain as a term if the name is generic.

### Step 2 — Identify + research (fully autonomous)
WebSearch the name plus every hint in the user's message (city, sector, raise amount, lead
investor are strong disambiguators); WebFetch the press release and company site. Nail down:
- **Canonical domain** — beware fresh rebrands: Inner Logic's PR pointed at
  semaphorsurgical.com but the live domain was innerlogic.io (the old site just redirects).
  If a news page 403s (finsmes, citybiz, businesswire sometimes do), try another outlet.
- HQ city/state/country; what they do; funding event (amount, date, round, lead +
  participating investors); leadership; **employee count only if sourceable** — never guess a
  number, leave blank otherwise; LinkedIn URL if it surfaces.
- Only if the name is genuinely ambiguous (multiple plausible companies, no disambiguating
  context — "Atoms" alone could be the shoe brand) → stop and ask before writing anything.

### Step 3 — Catch-all probe
```
python3.14 scripts/catchall_probe.py --domain <domain> --json
```
`catch_all` → option `194`; `invalid` (server does real per-mailbox checks) → `195`;
probe error/unknown → `193`. Catch-all domains need the MV→Reoon waterfall downstream — say so
in the report.

### Step 4 — Create the org (one POST, everything at once)
```
python3.14 skills/pipedrive/scripts/pipedrive_call.py POST /organizations --body '{
  "name": "<Name>", "website": "https://<domain>",
  "address": "<City, State, Country>", "employee_count": <n-if-sourced>,
  "6c03ee8ae1fd876379a086dfbafc911148124f25": "<Industry>",
  "997336778f6e562b0d0db2578037c30125e72f85": "<State>",
  "593adc3bc1f56d0bb9fba9852e08bee9a08e351a": "<City>",
  "d8a4cf212fe697b6aa8033615631deacbe0b571e": "Yes",
  "487087662337240aabd62722e8828f8811d6b17e": 196,
  "0244bd942390dee51e95158225e81af9b2438de4": [194],
  "f7fbc5d7c7622e9d6b42dcc1b12c25fa91e59725": "<description>"
}'
```
Omit ICP / Find People keys when the user said not to set them. Capture the returned `data.id`.

### Step 5 — LinkedIn fill (waterfall; never overwrites)
```
python3.14 skills/org-research-agent-v2/scripts/linkedin_fill.py \
    --org-id <id> --company "<Name>" --domain <domain>
```
Reports `filled` (+source), `clay_pending`, or `not_found` (leave blank; Clay may backfill).

### Step 6 — Company Research markdown
Render in the org-research-agent-v2 style — do NOT restate structured fields (org id,
industry, state, headcount, website live in columns):
```
# <Name>

_Researched YYYY-MM-DD._

- **Company Niche** — what they make/do, for whom, positioning.
- **<Product / Portfolio / Programs>** — sector-appropriate specifics.
- **Recent News & Funding (YYYY)** — the raise: date, amount, lead, participants, use of funds.
- **Company Size & Overview** — HQ, leadership, headcount if sourced ("not sourceable" if not).
- **Supply-Chain / Industry Pressures (YYYY)** — what their growth implies they need to buy.

## Personalization Points
- 3–5 outreach hooks (fresh raise, scale-up numbers, named programs, pedigree).
```
Write the file to the scratchpad, then PUT it:
```
python3.14 - <<'EOF'
import sys, json, subprocess
from pathlib import Path
md = Path(r"<scratchpad>\<slug>_research.md").read_text(encoding="utf-8")
body = json.dumps({"70c38286d0d6726717df98641a510c789a813afd": md})
r = subprocess.run([sys.executable, "skills/pipedrive/scripts/pipedrive_call.py",
                    "PUT", "/organizations/<id>", "--body", body], capture_output=True, text=True)
print("write ok:", '"success": true' in r.stdout)
EOF
```

### Step 7 — EXISTING org: update in place
`GET /organizations/<id>`, inspect the Step-4 target fields, then **write only the EMPTY ones**
in one PUT (plus ICP/Find People if unset). Never overwrite a non-empty value.
Company Research non-empty → **append** instead of replace (idempotent — skip when the
date-marker already exists):
```
- **Update YYYY-MM-DD — <headline>** — <one paragraph of fresh news>.
```
(Sila 57043 example: only Find People + Catch-All were empty; research got a $300M-raise
update bullet.)

### Step 8 — G:\ index row
New orgs only (skip if the org id already appears in the index):
```
echo "| <id> | <Name> | YYYY-MM-DD | standalone | Yes | <Industry> | (Pipedrive Company Research) |" \
  >> "/g/Shared drives/Capy Outreach/Organizations/_index.md"
```
Use `-` in the ICP column when ICP wasn't set.

### Step 9 — Report (once per batch)
One summary table — Org | ID (new/existing) | key facts | catch-all | LinkedIn — then notes:
fields left blank and why (e.g. employee count not sourceable), catch-all domains needing the
validation waterfall, anything ambiguous, and source links.

## Error handling
- `HTTP 401` on search → query string went into the path; redo with `--query`.
- POST 400 `ERR_SCHEMA_VALIDATION_FAILED` naming a key → that custom field was deleted; drop
  the key, retry, and flag the stale reference.
- Ambiguous company identity → ask; never create a best-guess org for the wrong company.
- Probe/LinkedIn failures are non-fatal — write `193` / leave blank and note it in the report.

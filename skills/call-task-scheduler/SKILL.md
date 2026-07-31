---
name: call-task-scheduler
description: Call-task workflow for principals - a Google Sheet checkbox creates Pipedrive call tasks for an org's ICP-Yes people (Apps Script webhook, no local dependency), and a daily claude.ai cloud routine moves completed first calls into the principal's HotHawk voicemail sequence (list + campaign + "voicemail" tag) and pushes phone-less people/thin orgs to Clay enrichment tables. Use when the user says "create call tasks for <principal>", "call task scheduler", "voicemail sequence", "reconcile call tasks", "the call sheet", or asks about the Clay no-phone enrichment flow. Local Python scripts in scripts/ are the manual fallback and reference implementation.
---

# Call Task Scheduler (v2 — Sheet-triggered, cloud-run)

## Architecture

```
Google Sheet "Call Task Scheduler" (Requests tab)
  └─ tick "Create" ─► Apps Script (apps-script/Code.gs, runs in Google cloud)
       ├─ Pipedrive LIVE: org → ICP-Yes people → 3 call activities per person with phone
       │    (due next business day / +5bd / +7bd, owner from the row)
       ├─ no-phone people → org note "@owner" + Clay People webhook
       ├─ org with <5 callable people → Clay Company webhook
       └─ writes ok/error + summary back into the row (Status / Result)

LOCAL scheduled task "CallTaskReconcile_Daily" (Mon-Fri 08:20) — scripts/scheduled/
  └─ reconcile_and_load.py: DONE call tasks from the last 3 business days (title contract)
       → per person, first time only: HotHawk find/create company+lead → list
         `voicemail-called-<slug>` → attach list to the principal's Voicemail campaign →
         ENROL the leads → tag `voicemail`
       → sibling call tasks are LEFT OPEN; failures log + alert Discord (notify_run_failure.py)
```

**2026-07-30: the reconcile moved OFF the claude.ai cloud routine.** That routine
(`trig_01JhnGNSJ2bSkRLZ7b236rPK`) is now **disabled**, kept only as a reference for its prompt.
It failed silently three times — dead since ~7/10, missed 7/14's 65 done calls, and loaded zero
people 7/28–7/30 while still reporting green — because nothing local ever saw its exit code and
it left no artifact whose absence anyone would notice. The local task runs the same scripts a
human runs by hand: it writes `_reconcile_*.json` artifacts, logs to `scripts/scheduled/logs/`,
and pings Discord on failure. It runs 30 min before `CallLoadAudit_Daily`, so the audit
independently re-checks each morning's work. Don't re-enable the cloud routine without deciding
which one owns the job — two owners double-write.

- Sheet: https://docs.google.com/spreadsheets/d/1apeJni_cb86f_J5L_Y2UNrd1Xq1nA2iD8exQQBxIM6A
- The **Registry tab** in that sheet is the source of truth for principals:
  `slug | display_name | hothawk_workspace_id | voicemail_sequence_id | sequence_name`.
  `references/voicemail-sequences.json` is a committed mirror — keep both in sync.
- New principal rule: the voicemail sequence is ALWAYS the campaign whose name contains
  "Voicemail" in the client's HotHawk workspace (`GET /v1/campaigns?workspaceId=`); auto-resolve,
  ask the user only on zero/multiple matches. (Franklin's lives in its own "Franklin Casting"
  workspace — never assume the client's own workspace.)

## Contracts (do not change without updating Apps Script + routine + scripts together)

- Activity subject: `<Principal display name>: Call <n> - <Last Name> from <Organization>`
  — principal-first since 2026-07-10 (rep feedback: group tasks by principal at a glance).
  This IS the machine marker; n∈1..3; principal resolved by display name via the registry.
  The legacy pre-2026-07-10 format `Call <n>: <Last> from <Org> for <Display>` is still
  parsed everywhere (rotation scan + reconcile) for the 45-day window.
- Person on the activity = primary participant (v2 API rejects person_id on create).
- Note (human-only): `Call <Full Name> - <Title> @ <phones>`; Call 1 additionally carries
  `[Clicking "Mark As Done" moves lead to a Voicemail Email Sequence in HotHawk]`.
- Only the FIRST completed call per person triggers the HotHawk add; skip if the lead already
  exists in the workspace tagged `voicemail`. **Remaining open call tasks stay OPEN** (Marcella
  2026-07-15). `--close-siblings` opts into marking them done with an audit note (never deleted);
  it is OFF by default in both scripts since 2026-07-30. The default used to be the other way and
  the 07-30 catch-up auto-closed 59 tasks before anyone noticed — leave it off.
- People with no email are reported, not subscribed.
- **Territory gate** (2026-07-08): out-of-territory people are skipped ENTIRELY (no tasks, no
  Clay). Rules mirror `go-capy-outreach/shared-references/client-territories.json` (embedded in
  Code.gs keyed by display name; Python imports `territory_filter.py`): Patriot Forge = US
  WA/OR/CA/AZ/NV only; Tech-Max excl FL/MA/IL/PA/VT; General Foundry excl NV/UT/CO/MA/CT;
  Harvey Vogel SoCal-only (strict); Megatech US-only. Location from person Contact
  State/City/Country fields, org Company State fallback; unknown location keeps (except strict).
- **Title tiers + 25-person cap + 45-day rotation** (2026-07-08): tier 1 = functional keyword
  (sourcing/commodity/purchasing/supplier/procurement/supply chain/category) + seniority
  (manager/sr/senior/director), excluding program managers, engineers, buyers, specialists,
  entry-level, VP, C-level; tier 2 = VP with the same keywords (used only if tier 1 < 25);
  tier 3 = the rest (skipped entirely when org has >100 phoned people). Max 25 people per run;
  the recent-task scan covers OPEN + DONE-within-45-days, so a re-run rotates to the next 25.
- **Pacing**: 3 Call-1s per company per business day (5 → 3 per rep feedback 2026-07-10;
  priority order); each person's Call 2/3 = +5/+7 business days from their own Call 1.
- **Phone format gate** (2026-07-10): a person whose every phone fails `phone_format_ok`
  (strip `ext/x/#` extensions; accept 10 digits, 1+10(+glued ext), `+`international 8-15) is
  moved to the no-phone bucket BEFORE the 25-cap selection, so garbage numbers never consume a
  slot. Snapshot trial: excludes 0.43% of phoned people, all genuinely uncallable strings.
- **Pre-call verification sweep** (`scripts/verify_call_contacts.py`, run daily): covers open
  Call-1s due in the next 3 business days only (never the whole org). LinkedIn present →
  Bright Data enrich; departed → flag + auto-close the person's open call tasks (audit note,
  never delete) so re-runs rotate replacements in; title drift → informational flag. LinkedIn
  missing → Clay People push (tier-1/CLAY_EXCLUDE/≤10 guardrails; table's LinkedIn data point
  writes back). Valid phones → standalone Clay phone-validation webhook (`phone_validation` in
  references/clay-webhooks.json; null until the table is built — free format check still runs).
  Idempotent via a `[verified YYYY-MM-DD]` note marker; Bright Data spend capped 25/sweep.
- **Clay People push throttle**: only tier-1-title no-phone people, max 10 per run, minus the
  **CLAY_EXCLUDE blocklist** (2026-07-08): program manager, contract(s) manager, machine operator,
  engineer (any), production manager, planner (any), materials coordinator, facilitator,
  investment casting, area manager, subcontract. Exception: any title containing
  "supplier development" is always eligible, even with "engineer" in it. The blocklist is a hard
  gate — it applies even when the org has ZERO people with phone numbers.
- **Blank-ICP people are classified from their job title at run time** (rules ported verbatim
  from `people-icp-classifier`: default-Yes; positive procurement/sourcing/buyer/supply-chain
  keywords beat overlapping excludes; negative role keywords → No; empty title stays blank) and
  the verdict is WRITTEN BACK to Pipedrive (v1 `PUT /persons/{id}`). Keep the keyword lists in
  Code.gs and create_call_tasks.py in sync with that skill.
- **Clay second pass**: when a run pushes people to the Clay People table, the Apps Script
  schedules a one-shot re-run of the row ~12 min later — Clay writes found phones back to
  Pipedrive within ~5 min, and the re-run creates tasks for the newly-phoned people only
  (idempotency gate). The second pass never re-pushes to Clay.
- HotHawk lead create ignores name-splitting: PATCH firstName/lastName after create.
- Clay pushes (references/clay-webhooks.json): no-phone ICP-Yes in-territory tier-1 people not
  on the CLAY_EXCLUDE blocklist → People table; every org with fewer than 5 callable people →
  Company table.

## Pieces

- `apps-script/Code.gs` — the whole of Workflow A. Paste into the sheet's Apps Script editor;
  set Script Property `PIPEDRIVE_API_TOKEN`; run `setup()` once (builds tabs/dropdowns/trigger).
- `scripts/reconcile_and_load.py` + `scripts/scheduled/` — Workflow B, now the PRIMARY path.
  `register_scheduler.ps1` registers `CallTaskReconcile_Daily` (Mon-Fri 08:20 → run_reconcile.ps1
  → reconcile_and_load.py). `--lookback N` business days (default 3) self-heals a failed day;
  `_reconcile_state.json` keeps it idempotent. Exit 1 (step failed, or `RESULT: partial` from a
  broken campaign) triggers the Discord alert. `--dry-run` plans without writing.
- claude.ai routine "Call Task Reconcile" — DISABLED 2026-07-30, superseded by the above.
- `scripts/` — Python manual fallback (same logic, snapshot-based reads via the outreach
  plugin's pd_cache): `create_call_tasks.py`, `reconcile_done_calls.py`, `hothawk_subscribe.py`,
  `business_days.py`. Run with `--apply` after a dry-run; `--test` = one activity trial.
  `reconcile_done_calls.py --since YYYY-MM-DD` (2026-07-16) reconciles a date RANGE (through
  `--date`/today) — ALWAYS use it for catch-ups after missed days; single `--date` catch-ups are
  how the 7/10+7/13 done calls fell through and never reached HotHawk.
  `hothawk_subscribe.py` (2026-07-16) skips leads whose email has no `@` (reports the person id
  for a PD fix) and isolates campaign failures per principal group (deleted campaign → 404 /
  400 "Internal server error"; leads stay on the list, persons NOT marked, RESULT: partial)
  instead of aborting the run. Franklin's old sequence 47e2d481-… was DELETED in HotHawk —
  registry + routine now point at d5d9f027-… (draft; activate then enroll).
  `retrofit_calls.py` was the one-off 2026-07-08 cleanup that applied territory/cap/pacing to
  pre-existing open tasks (kept as a template for future rule retrofits).

## Verified API facts (2026-07-07)

- Pipedrive v2 `POST /activities` requires `participants:[{person_id,primary:true}]`.
- Pipedrive v2 activities list has NO type/subject filter — filter client-side; paginate by cursor.
- HotHawk REST (https://api.hothawk.ai/v1, Bearer HOTHAWK_API_TOKEN; spec at
  https://api.hothawk.ai/docs/public-json): `GET/POST /contacts-leads`,
  `PATCH /contacts-leads/{id}`, `GET/POST /contacts-companies` (companyId required on lead, from
  domain), `GET/POST /contacts-lists`, `PUT /contacts-lists/{id}/contacts
  {selectionType:"ids",leadIds}`, `POST /campaigns/{id}/lists
  {listSelection:{selectionType:"ids",listIds}}` (idempotent append), `GET/POST /contacts-tags`,
  `PUT /contacts-tags/{id}/contacts`. Campaigns and subsequences share ids.
  **Route rename 2026-07-30**: the whole `/crm/*` contacts family became `/contacts-*` and the
  member sub-routes `/{id}/leads` became `/{id}/contacts`; bodies are unchanged and the old paths
  now 404. This broke `hothawk_subscribe.py` outright and silently broke the call-load audit's
  per-lead re-check (its `except` swallowed the 404, so every lead read as NOT loaded). Both are
  fixed. `campaign-lead-uploader/scripts/hh_upload.py` still calls the dead `/crm/lists` and
  `/crm/leads/bulk` (now `/contacts-lists`, `/contacts-imports/bulk`, `/contacts-imports/{id}`).
- Pipedrive owner ids: Marcella 22638704, Jonathan 20845253, Sam 20845572, Ericka 23490137.
- Field keys: Person ICP `1a8684b9333f530c727f9bff307391d3d200c897`, Job Title
  `ef54f66e8242d193fd263fa16ac83850271b2794`, LinkedIn `cf2472711fcbe2a22cef32aea82f1a5a555761a8`.

## Output

Scripts end with `RESULT: ok|error - <details>`; the Apps Script writes the same summary into the
row's Result cell; the routine ends with a one-line summary on its claude.ai page.

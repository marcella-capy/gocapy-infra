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

claude.ai cloud routine "Call Task Reconcile" (weekday mornings)
  └─ yesterday's DONE call tasks (title contract) → per person, first time only:
       HotHawk: find/create company+lead → list `voicemail-called-<slug>` → attach list to the
       principal's Voicemail campaign → tag `voicemail` → auto-close the person's other call tasks
```

- Sheet: https://docs.google.com/spreadsheets/d/1apeJni_cb86f_J5L_Y2UNrd1Xq1nA2iD8exQQBxIM6A
- The **Registry tab** in that sheet is the source of truth for principals:
  `slug | display_name | hothawk_workspace_id | voicemail_sequence_id | sequence_name`.
  `references/voicemail-sequences.json` is a committed mirror — keep both in sync.
- New principal rule: the voicemail sequence is ALWAYS the campaign whose name contains
  "Voicemail" in the client's HotHawk workspace (`GET /v1/campaigns?workspaceId=`); auto-resolve,
  ask the user only on zero/multiple matches. (Franklin's lives in the shared "Bottom Shelf"
  workspace — never assume the client's own workspace.)

## Contracts (do not change without updating Apps Script + routine + scripts together)

- Activity subject: `Call <n>: <Last Name> from <Organization> for <Principal display name>`
  — this IS the machine marker; n∈1..3; principal resolved by display name via the registry.
- Person on the activity = primary participant (v2 API rejects person_id on create).
- Note (human-only): `Call <Full Name> - <Title> @ <phones>`; Call 1 additionally carries
  `[Clicking "Mark As Done" moves lead to a Voicemail Email Sequence in HotHawk]`.
- Only the FIRST completed call per person triggers the HotHawk add; skip if the lead already
  exists in the workspace tagged `voicemail`. Remaining open call tasks are marked done with an
  audit note — never deleted.
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
- **Pacing**: 5 Call-1s per company per business day (priority order); each person's Call 2/3 =
  +5/+7 business days from their own Call 1.
- **Clay People push throttle**: only tier-1-title no-phone people, max 10 per run.
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
- Clay pushes (references/clay-webhooks.json): every no-phone ICP-Yes person → People table;
  every org with fewer than 5 callable people → Company table.

## Pieces

- `apps-script/Code.gs` — the whole of Workflow A. Paste into the sheet's Apps Script editor;
  set Script Property `PIPEDRIVE_API_TOKEN`; run `setup()` once (builds tabs/dropdowns/trigger).
- claude.ai routine "Call Task Reconcile" — Workflow B, managed via the RemoteTrigger API.
- `scripts/` — Python manual fallback (same logic, snapshot-based reads via the outreach
  plugin's pd_cache): `create_call_tasks.py`, `reconcile_done_calls.py`, `hothawk_subscribe.py`,
  `business_days.py`. Run with `--apply` after a dry-run; `--test` = one activity trial.
  `retrofit_calls.py` was the one-off 2026-07-08 cleanup that applied territory/cap/pacing to
  pre-existing open tasks (kept as a template for future rule retrofits).

## Verified API facts (2026-07-07)

- Pipedrive v2 `POST /activities` requires `participants:[{person_id,primary:true}]`.
- Pipedrive v2 activities list has NO type/subject filter — filter client-side; paginate by cursor.
- HotHawk REST (https://api.hothawk.ai/v1, Bearer HOTHAWK_API_TOKEN; spec at
  https://api.hothawk.ai/docs/public-json): `GET/POST /crm/leads`, `PATCH /crm/leads/{id}`,
  `GET/POST /crm/companies` (companyId required on lead, from domain), `GET/POST /crm/lists`,
  `PUT /crm/lists/{id}/leads {selectionType:"ids",leadIds}`, `POST /campaigns/{id}/lists
  {listSelection:{selectionType:"ids",listIds}}` (idempotent append), `GET/POST /crm/tags`,
  `PUT /crm/tags/{id}/leads`. Campaigns and subsequences share ids.
- Pipedrive owner ids: Marcella 22638704, Jonathan 20845253, Sam 20845572, Ericka 23490137.
- Field keys: Person ICP `1a8684b9333f530c727f9bff307391d3d200c897`, Job Title
  `ef54f66e8242d193fd263fa16ac83850271b2794`, LinkedIn `cf2472711fcbe2a22cef32aea82f1a5a555761a8`.

## Output

Scripts end with `RESULT: ok|error - <details>`; the Apps Script writes the same summary into the
row's Result cell; the routine ends with a one-line summary on its claude.ai page.

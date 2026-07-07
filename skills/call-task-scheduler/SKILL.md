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

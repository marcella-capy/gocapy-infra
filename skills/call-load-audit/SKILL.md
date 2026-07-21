---
name: call-load-audit
description: Audits whether call-task-scheduler calls that a rep marked DONE actually got enrolled (and sending) in the matching HotHawk voicemail campaign, and pings a summary to the AISDR Discord. Runs daily on weekdays (previous business day), weekly on Friday morning (the week so far), and monthly on the 1st (the whole previous month). Use when Marcella asks "did yesterday's/this week's/last month's done calls all load into HotHawk?", to (re)install the schedule, or to run an ad-hoc audit for a date/window.
---

# call-load-audit

## What it answers
Of the [call-task-scheduler](../call-task-scheduler/SKILL.md) calls a rep marked **DONE** in a window,
did every person actually get **enrolled (and sending)** in their principal's HotHawk *Post-Voicemail*
campaign? A done call is supposed to trigger a HotHawk subscribe (call-task-scheduler Workflow B). This
skill independently re-checks the **outcome** so a silently-dead reconcile — or a **draft campaign that
holds leads on the list but never sends** — gets caught, not discovered weeks later.

It is deliberately **independent of `_reconcile_state.json`**: it reads two live sources and compares them.
- **DONE set** — Pipedrive done call activities matching the call-task-scheduler subject contract, in the
  window (via `create_call_tasks.principal_of`).
- **LOADED set** — leads present in the principal's campaign via `GET /campaigns/{seq}/leads`. A lead only
  appears there once **enrolled**; a draft campaign returns none, so those done calls read as NOT loaded.

## Report (posted to Discord — `DISCORD_AISDR_WEBHOOK_URL`)
Roster table, one row per **principal / User (Ericka/Mark/Jon/…) / Company Name / # of Calls**, where
User is the Pipedrive activity `owner_id` (the rep who owns the call) and "# of Calls" counts done call
activities in the window. A `TOTAL <User>` footer at the bottom sums calls per rep. Below it, a **NOT LOADED** section lists each person whose done call never
reached HotHawk (with the reason: `no-email`, `not-enrolled (on list only / draft campaign?)`, or
`no-sequence-in-registry`). Headline shows `N calls / P people · L loaded, M missing`.

## Run it
```
cd scripts
python audit_call_loads.py --mode day            # previous business day (prints, no post)
python audit_call_loads.py --mode week           # Mon..previous-business-day of this week
python audit_call_loads.py --mode month          # the whole previous calendar month
python audit_call_loads.py --mode day --discord  # also post to the AISDR Discord
python audit_call_loads.py --mode day --date 2026-07-16   # override "today" for backfills/tests
```
- Reads `HOTHAWK_API_TOKEN` and `DISCORD_AISDR_WEBHOOK_URL` from capy_env; the Pipedrive helpers,
  registry (`../call-task-scheduler/references/voicemail-sequences.json`), USERS map, and the
  person/org snapshot (`pd_cache`) are reused from the sibling skills — nothing is duplicated.
- "Loaded correctly" = **enrolled & sending** (present in `/campaigns/{seq}/leads`), per Marcella —
  strict enough to catch the draft-campaign trap.

## Schedule (Windows Task Scheduler)
Local scheduler chosen deliberately: the audit exists to catch a dead cloud reconcile, so it must not
share that failure mode. Install / re-install (idempotent):
```
scripts\scheduled\register_scheduler.ps1
```
Registers three wake-to-run tasks (distinct start minutes, `StartWhenAvailable` so an off-at-trigger
machine catches up):
- **CallLoadAudit_Daily** — Mon–Fri 08:50 → `--mode day`
- **CallLoadAudit_Weekly** — Fri 09:12 → `--mode week`
- **CallLoadAudit_Monthly** — 1st 07:42 → `--mode month` (calendar trigger via task XML)

Logs land in `scripts/scheduled/logs/audit_{day,week,month}_YYYYMMDD.log`. The laptop must sleep, not
shut down, for wake-to-run to fire (see the quiet-workday scheduler note).

## Notes / gotchas
- Matches the registry **Post-Voicemail** sequence only. A person enrolled in some *other* campaign in
  the same workspace still reads as "not loaded" here — correct, because the done call's job is to load
  them into the voicemail sequence specifically.
- Emails without an `@` (e.g. the literal `invalid` placeholder in Pipedrive) are reported as `no-email`.
- `GET /campaigns/{id}/leads` caps `limit` at 25 — the script paginates on `meta.pagesCount`.
- People newer than the daily `pd_cache` snapshot are fetched live from Pipedrive as a fallback.

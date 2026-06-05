---
name: inbox-placement
description: >
  Run and read PlusVibe inbox-placement (deliverability/spam-placement) tests for a Go Capy
  client (principal). Use when the user wants to "run an inbox placement test", "check
  deliverability / inbox vs spam", "are our emails landing in spam", "create a placement
  test for <client>", or "how's placement for <principal>". A placement test sends seed
  emails from the principal's sending accounts to a seed panel and reports Inbox vs Spam vs
  Promotions; PlusVibe runs them as AUTOMATIC parent tests that rotate across the principal's
  accounts. This is the FINAL post-onboarding/warmup step and is intentionally a SEPARATE
  skill from `mailbox-onboarder`. Triggers on "inbox placement", "deliverability test",
  "placement test", "inbox vs spam".
---

# Inbox Placement

Per-principal deliverability testing on PlusVibe. Run this **after** mailboxes are onboarded
(via `mailbox-onboarder`) and warmup has progressed — placement reflects real sending health,
so it's the last step, not part of onboarding.

## Concept
- A **parent test** (type `AUTOMATIC`) belongs to a principal and **rotates across that
  principal's sending accounts**, mailing a seed panel and recording where each lands.
- Results: `sent`, `inbox`, `spam`, `promotion`, `missing`, plus per-sub-test `inbox_r` rates.
- One parent test per principal per period is the convention; name them `"<Principal> - <Month>"`
  (e.g. `Alpha Grainger - May`, `Tech-Max - Jun`).

## Workspaces
PlusVibe placement is per workspace (placement tests live alongside the accounts):
- Machining `69fd080546e55fcda1d94da6` (TMX, Megatech, LNP, Alpha Grainger)
- Forge + Casting `69fa2d9be1623d61f71e9ded` (Patriot, Shellcast, General Foundry, VRC, Harvey Vogel, Franklin, USAI)

## Golden template (the source of truth for config)
The create API only accepts `name` + `type` — it **cannot** set send-as-plain-text, the weekday
schedule, the start date, account-selection (Random/tag/5%), the linked campaign, or automation-off.
Those are **UI-only**. So we configure ONE test by hand and clone it for every other principal.

**Golden template = `Tech-Max - Jun`** (machining ws, id `6a22dcf1517396fcbf77874f`), configured in
the UI as: send-as-plain-text **ON**, schedule **Mon–Fri**, account selection **Random**, sample **5%
of the tag**, **email automation OFF**, start date set. **Verified 2026-06-05: a `duplicate` of it
inherits ALL of those toggles.** Keep this test as the template; if it's deleted, re-designate a freshly
configured one here.

## Workflow (per principal) — duplicate, don't create
1. **Check existing tests** — make sure the principal doesn't already have a current one:
   ```
   py scripts/inbox_placement.py -w machining report --name-contains "Alpha Grainger"
   ```
2. **Duplicate the golden template** into `"<Principal> - <Month>"` (inherits all the config toggles):
   ```
   py scripts/inbox_placement.py -w machining duplicate \
       --parent-test-id 6a22dcf1517396fcbf77874f --name "Alpha Grainger - Jun"
   ```
   The copy lands as **`DRAFT`** and still points at the **template's TMX tag + TMX campaign** — that's
   the only thing that's wrong on the clone.
3. **Re-point + start in the PlusVibe UI (the ONLY manual step — API can't set these):**
   - **Email accounts / tag** → change from TMX to **this principal's tag** (selection stays Random, 5%).
   - **Campaign** → pick a campaign belonging to this principal.
   - Confirm **start date** (today) and then **Start** the test (a duplicate stays `DRAFT` until started).
   - Everything else (plain-text, Mon–Fri, automation-off) already carried over — just verify.
   > 5% of the tag's accounts (PlusVibe guidance: only 3–5% — testing every account causes content
   > fatigue, wastes send volume, and hurts sender reputation). Cross-workspace: forge principals
   > duplicate a forge-ws golden test (`-w forge`); you can't clone across workspaces.
4. **Read results** — once it's `COMPLETED`, `report` shows the inbox/spam/promo summary; for a
   single sending account's detail use:
   ```
   py scripts/inbox_placement.py -w machining result --test-id <id> --sender-acc-id <acct id>
   ```
5. **Act** — if spam/promotion is high or inbox rate drops, pause/slow that principal's sending,
   keep warming, and consider the bounce/blocklist hygiene in the AI-SDR pipeline.

## Script — `scripts/inbox_placement.py`
Self-contained (reads `~/.claude/global.env` for `PLUSVIBE_API_KEY`). Subcommands:
`list` (raw/`--json`), `create` (`--name`, `--type`), **`duplicate` (`--parent-test-id`, `--name`)**,
`result` (`--test-id`, `--sender-acc-id`), `report` (human summary, optional `--name-contains`). Wraps
`/email-placement/{list/parent-tests, create/parent-test, duplicate/parent-test, get/test-automatic-result}`
(same API the `plusvibe-api` skill's `email_placement.py` uses). **Prefer `duplicate` over `create`** —
`create` makes a bare shell needing full UI setup; `duplicate` of the golden template carries the config.

## Notes
- Needs outbound network to `api.plusvibe.ai`; if the Bash sandbox blocks egress, run unsandboxed.
- Placement tests consume seed-panel sends — one per principal per period, don't spam-create.
- **Sample only 3–5% of a principal's accounts per tag** (PlusVibe guidance) — never test every account
  (content fatigue + reputation risk).
- The API can't set tag/campaign/schedule/plain-text/automation/sample — those are **UI-only**, which is
  why the workflow clones a fully-configured **golden template** and only re-points tag + campaign in the UI.
- A `duplicate` lands as `DRAFT` and inherits the template's (wrong) tag+campaign — always re-point + start.
- Run **after warmup has matured** — early numbers are a baseline, not the verdict.
- Verified 2026-06-05: duplicating the configured `Tech-Max - Jun` carried over every toggle.

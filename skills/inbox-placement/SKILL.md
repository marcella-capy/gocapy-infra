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

## Workflow (per principal)
1. **Check existing tests** — `report` (or `list`) and see if a current parent test exists for the principal.
   ```
   py scripts/inbox_placement.py -w machining report --name-contains "Tech-Max"
   ```
2. **Create one if needed** — AUTOMATIC, named `"<Principal> - <Month>"`:
   ```
   py scripts/inbox_placement.py -w machining create --name "Tech-Max - Jun" --type AUTOMATIC
   ```
   The AUTOMATIC test rotates the principal's accounts itself. (How many accounts it samples per
   run is governed by PlusVibe; confirm in the UI if a specific rotation breadth is required.)
3. **Read results** — once it's `COMPLETED`, `report` shows the inbox/spam/promo summary; for a
   single sending account's detail use:
   ```
   py scripts/inbox_placement.py -w machining result --test-id <id> --sender-acc-id <acct id>
   ```
4. **Act** — if spam/promotion is high or inbox rate drops, pause/slow that principal's sending,
   keep warming, and consider the bounce/blocklist hygiene in the AI-SDR pipeline.

## Script — `scripts/inbox_placement.py`
Self-contained (reads `~/.claude/global.env` for `PLUSVIBE_API_KEY`). Subcommands:
`list` (raw/`--json`), `create` (`--name`, `--type`), `result` (`--test-id`, `--sender-acc-id`),
`report` (human summary, optional `--name-contains`). Wraps the PlusVibe endpoints
`/email-placement/{list/parent-tests, create/parent-test, get/test-automatic-result}`
(same API the `plusvibe-api` skill's `email_placement.py` uses).

## Notes
- Needs outbound network to `api.plusvibe.ai`; if the Bash sandbox blocks egress, run unsandboxed.
- Placement tests consume seed-panel sends — create per principal per period, don't spam-create.
- Verified working: an existing `Alpha Grainger - May` AUTOMATIC test returns inbox 8/8, spam 0.

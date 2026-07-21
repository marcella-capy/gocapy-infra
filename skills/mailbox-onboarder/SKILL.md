---
name: mailbox-onboarder
description: >
  Onboard cold-email sending mailboxes for a Go Capy client (principal) into PlusVibe
  AND HotHawk, in one credential-verified batch. Use whenever the user wants to "add
  mailboxes / email accounts to PlusVibe", "onboard sending accounts", "add the new
  emails to PlusVibe and HotHawk", "set up the warmup for these mailboxes", or hands a
  client + a list of addresses (or a CSV with email/password) that already exist in
  SiteGround. The mailboxes MUST already be created in SiteGround first (use the
  `siteground` skill). This skill: (0) verifies every password with a single IMAP+SMTP
  login, (1) bulk-adds the passing mailboxes to PlusVibe with the canonical delivery +
  warmup settings, (2) sets the client signature, (3) assigns the client/principal tag,
  (4) bulk-adds the same mailboxes to HotHawk, (5) ensures the warmup tag in HotHawk,
  (6) enables warmup in PlusVibe. Also ships `warmup_stats.py` for periodic warmup
  health checks. Inbox-placement testing is a SEPARATE skill (`inbox-placement`), run
  later. Triggers on "mailbox onboarder", "onboard mailboxes", "add accounts to plusvibe/hothawk".
---

# Mailbox Onboarder

Adds a batch of **already-created SiteGround mailboxes** to both outreach platforms with
verified credentials and the standard Go Capy settings. The non-negotiable rule:
**never add a mailbox anywhere until its password passes a one-shot IMAP+SMTP login.**
This prevents the HotHawk `GATHERING` stall and the SiteGround IP-block that wrong
credentials cause (silent auth retries against a host that doesn't whitelist our IPs).

### ⚠️ The host, not just the password, must match (domain-match gate)
A confirmed root cause of HotHawk `GATHERING` stalls is a **wrong host**, not a wrong
password: onboarding CSVs sometimes list an `imap_host`/`smtp_host` that points at the
wrong domain (observed 2026-06-05 — every "Stephanie" row listed
`imap_host=mail.techmaxmfg.com` regardless of the address's actual domain; IMAP auth was
rejected there even though the password was valid on the address's own server). **The
address's own-domain host `mail.<domain>` is authoritative; treat the CSV's host columns
as untrusted.** `check_login.py` enforces this: if a protocol fails on the provided host
and that host differs from `mail.<domain>`, it retries that protocol once on the domain
host and reports the correction. **Always add to PlusVibe/HotHawk using the host that
`check_login` actually verified (the corrected `mail.<domain>`), not the CSV value.**

## Prerequisites (not in scope here)
- Mailboxes exist in **SiteGround** (the `siteground` skill creates them; default password
  `NewAirton@19642026!`). SiteGround is SSL-only: host `mail.<domain>`, IMAP 993 / SMTP 465,
  username = full email.
- You have the address list + password(s) — usually a CSV with `email` and `password` columns.
- `~/.claude/global.env` has `PLUSVIBE_API_KEY` (+ workspace ids) and `HOTHAWK_API_TOKEN`.

## Platforms & the invariant
- **PlusVibe** holds many clients per workspace, so **every account must carry its client
  (principal) tag** (table below). Two workspaces:
  - Machining `69fd080546e55fcda1d94da6` — tags: TMX `6a0cd81c4a80688441619120`,
    Megatech `6a15cb4f32bcd7444d83faad`, LNP `6a0f73e28e92e5feb7784d5a`,
    Alpha Grainger `6a0729a30793a969091e9dee`.
  - Forge + Casting `69fa2d9be1623d61f71e9ded` — tags: Patriot `6a0f68de271d1e58d2a41eb5`,
    Shellcast `6a15cb542aafd9e66d39358f`, General Foundry `6a15cb53a24347ba058c5d6f`,
    VRC `6a15cb522aafd9e66d39358e`, Harvey Vogel `6a15cb5033ec34ed7b197f48`,
    Franklin Casting `6a15cb572aafd9e66d393590`, USAI `6a1db20d9e4f8757a179b00a`.
- **HotHawk** uses one workspace per client (no client tag needed). Resolve the workspace id
  **live** via the HotHawk MCP `workspaces_short_list` (e.g. Tech-Max = `56508f2a-ed5f-44b9-a11f-d86b81c8f172`).
- **Invariant: PlusVibe ⊆ HotHawk** — every mailbox added to PlusVibe must also be in HotHawk.
  HotHawk may hold extra mailboxes that aren't in PlusVibe; that's fine.

## Canonical settings profile (hard-coded — do not improvise)
Source: client screenshots, 2026-06-04. Applies to every client.
- **Delivery:** `daily_limit=20`, `min_interval=20` (minutes), campaign ramp-up ON
  (`enable_camp_rampup=yes`, `camp_rampup_start=10`, `camp_rampup_increment=2`).
- **Warmup:** max daily `20` (hard cap 50), warmup ramp-up ON (start `10`, increment `2`),
  randomize ON `20%`, `warmup_business_type="Manufacturing Companies"`, schedule ON /
  timezone `America/Chicago (UTC-05:00)` / **weekdays only**, custom tracking domain OFF,
  warmup signature OFF, reply rate `35%`.
- **`warmup_custom_words`** is per-client: TMX/LNP/Alpha Grainger = `machining-parts`;
  Franklin/General Foundry/Harvey Vogel/Shellcast/Patriot = `casting-components`;
  VRC = `plastic-wonders`; USAI = `gasket-seals`. (Confirm from an existing account if unsure.)
- **Add with warmup DISABLED**, then enable it in the last step (step 6).

## Signatures
Per-client templates live in `gocapy-infra/shared-references/signatures/<slug>.md`. Each:
- starts with the BDR's **literal full name** (e.g. `Juliana Matos`) — NEVER the
  `{{sender_first_name}} {{sender_last_name}}` merge tags (Marcella's rule, re-confirmed 2026-07-20;
  each client has one fixed BDR, see the client → BDR table in the `siteground` skill). If a
  signature file still starts with merge tags, replace them with the client's BDR name before applying;
- contains exactly one of the **3 allowed phone numbers** baked in (`949-209-9625`, `949-524-5765`,
  `949-820-8005` — Marcella's live set 2026-07-17).
PlusVibe stores signatures as HTML — wrap the file's lines as `<div><br>line1<br>line2…</div>`.

## The batch workflow
Run as stages over the whole list (not one mailbox at a time).

### Step 0 — Credential pre-check (MANDATORY, first)
`scripts/check_login.py` does one IMAP + one SMTP SSL login per mailbox (no retry).
```
py scripts/check_login.py --csv <accounts.csv> --delay 6 --max-consecutive-timeouts 3
py scripts/check_login.py --email a@b.com --password 'pw'    # single
```
Needs outbound network (993/465); if the Bash sandbox blocks egress, run with the sandbox
disabled. **Drop and report any `auth rejected` FAIL** (mailbox deleted or wrong password — fix in
SiteGround); only PASS mailboxes continue. Never loop a failed login.

⚠️ **SiteGround brute-force protection — throttle big batches.** Too many failed logins from one IP
in a short window gets that **IP blocked** by SiteGround server protection (unblock via the SiteGround
support page → "I have other technical issues" → Unblock IP). A blocked IP then makes EVERY further
login **time out**. So for a large list:
- always pass `--delay` (e.g. 6s) and `--max-consecutive-timeouts 3` so the batch **auto-aborts** the
  moment the block trips instead of digging in deeper (exit code 2 = aborted);
- a **`connection error`/timeout is NOT a bad mailbox** — it means "couldn't reach the server" (likely
  the block). Only `auth rejected` proves a deleted/invalid mailbox. Treat timeouts as UNVERIFIED, never
  as failures or deletions;
- **skip mailboxes already known-good**: cross-reference HotHawk — any mailbox `CONNECTED` there is
  already proven valid, so don't re-test it (fewer logins = less block risk);
- if you do get blocked, wait for the unblock, then re-check only the still-unverified subset with `--delay`.

### Step 0.5 — Dedup pre-check (before ANY add)
List the existing emails on **both** platforms first and add only what's missing — do not rely on
platform-side dedupe. PlusVibe: `accounts.py list -w <ws> --tags <client>` (or `list_email_accounts`).
HotHawk: `mailboxes_list` for the client workspace. Compute `toPlusVibe`/`toHotHawk` independently (a
mailbox may already be in one platform but not the other). This makes duplicates impossible regardless
of platform behavior, and keeps re-runs idempotent.

### Step 1 — Bulk-add PASS mailboxes to PlusVibe (warmup disabled)
Build a JSON array of account objects (canonical delivery values, `enable_warmup=no`,
`warmup_custom_words=<client>`, host `mail.<domain>` 993/465, username/password = the address/pwd)
and call `accounts.py bulk-add`:
```
py <plusvibe-api>/scripts/accounts.py bulk-add -w <ws> --json accounts.json
```
(`<plusvibe-api>` = `go-capy-outreach/skills/plusvibe-api/scripts`.)

### Step 2 — Signature + warmup advanced config
`accounts.py bulk-update -w <ws> --ids <new ids> --signature "<html>" --fields warmup.json`
where `warmup.json` carries `warmup_max_daily_limit:20, warmup_initial_daily_limit:10,
warmup_pace_increment:2, bulk_warmup_is_slow_rampup:"yes", warmup_randomize:"yes",
warmup_randomize_num:20, warmup_business_type:"Manufacturing Companies", warmup_signature:"no"`.
(Reply rate is already 35 by default; leave it unless wrong.)

### Step 3 — Assign the principal/client tag
`accounts.py bulk-assign-tags -w <ws> --ids <new ids> --tag-id <client tag> --action ASSIGN`.

### Step 4 — Bulk-add the same mailboxes to HotHawk (REST API, not MCP)
Use the **REST API** (`POST /v1/mailboxes/connect-imap`) so this works in headless/cron runs too —
the HotHawk MCP server is only present in interactive sessions. Script:
`scripts/connect_hothawk.py` (`HOTHAWK_API_TOKEN` via `capy_env`):
```
py scripts/connect_hothawk.py --workspace-id <uuid> --csv <accounts.csv>   # email,password columns
```
`imapHost`/`smtpHost`=`mail.<domain>`, 993/465, username=email, password=verified pwd. The script
skips mailboxes already in the workspace (idempotent) and makes ONE login attempt each. **No separate
PlusVibe-auth check — step 0 already proved the credentials.** GATHERING is normal up to ~30 min; only
`GATHERING > 6 h` is the stall bug → stop, do not re-add in a loop. (The MCP `mailboxes_connect_imap_create`
tool remains a valid interactive alternative when the MCP server is connected.)

### Step 4.5 — Test send (SMTP smoke test)
PlusVibe has **no ad-hoc test-send endpoint** (only warmup + email-placement seed tests). To prove a
mailbox delivers, send one real email over its own SMTP via `scripts/test_send.py`:
```
py scripts/test_send.py --from <mailbox> --password 'NewAirton@19642026!' --to marcella@gocapy.com \
   --display-name "<BDR name>" --subject "<Client> test send (please ignore)"
```
One send from the first mailbox, no retries; confirm arrival (inbox vs spam).

### Step 5 — Ensure the warmup tag in HotHawk
Read the PlusVibe `warmup_custom_words` for the client; HotHawk `warmups_tags_list`, and
`warmups_tags_create` only if that value is missing (for TMX `machining-parts` already exists).
⚠️ Open question: no MCP endpoint binds a mailbox→warmup-tag; verify whether that linkage is
automatic on connect or must be set in the HotHawk UI, and record the answer here.

### Step 6 — Enable warmup in PlusVibe
`warmup.py bulk-update -w <ws> --ids <new ids> --status ACTIVE`.

Onboarding ends here. Run the **`inbox-placement`** skill later (after warmup has progressed).

## Monitoring — `scripts/warmup_stats.py`
Periodic warmup health for a principal:
```
py scripts/warmup_stats.py --workspace <machining|forge|id> [--tag <client tag>]
py scripts/warmup_stats.py --workspace machining --start-date 2026-05-28 --end-date 2026-06-04
```
Reports per-account warmup status + 7-day warmup health + warmup sent today, flags any below 90%,
and (with a date range) the inbox/spam/promotion aggregate.

## Reused scripts
`go-capy-outreach/skills/plusvibe-api/scripts/`: `accounts.py` (bulk-add / bulk-update /
bulk-assign-tags / status / list), `tags.py` (create-if-missing), `warmup.py` (bulk-update / stats / enable).
HotHawk side via its MCP server (`mailboxes_connect_imap_create`, `mailboxes_list`,
`warmups_tags_list/create`) — see the `hothawk-api` skill.

## Verification
- `check_login.py` on a known-good vs a wrong password → PASS/exit 0 vs FAIL/exit 1, one attempt each.
- After a batch: PlusVibe accounts have settings + signature + client tag; HotHawk shows them (CONNECTED
  within ~30 min); warmup tag present; warmup ACTIVE. Confirm PlusVibe ⊆ HotHawk for the client.

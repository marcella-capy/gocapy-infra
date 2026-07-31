---
name: hothawk-mailbox-connect
description: >
  Add sending mailboxes to a HotHawk workspace AND verify they actually connect — plus a
  daily workday scan that finds disconnected accounts across every workspace and reconnects
  or removes them. HotHawk shows a new mailbox as "Gathering data" even when its IMAP/SMTP
  credentials are WRONG; the bad mailbox then silently hammers auth against SiteGround, which
  doesn't whitelist us, so the HotHawk sending IP gets blocklisted. This skill defends against
  that: it watches each newly-added mailbox and deletes any that never reach CONNECTED within
  the threshold (stopping the auth retries), and the daily scan keeps dead accounts from
  piling up. Use for "add mailbox to hothawk", "add inbox to hothawk", "connect mailbox /
  inbox in hothawk", "check if the hothawk mailbox connected", "hothawk mailbox health",
  "scan disconnected hothawk mailboxes", or when uploading a batch of sending addresses for a
  client. Companion to hothawk-api (general wrapper); this skill owns the add + connection-health
  workflow. Adding mailboxes to HotHawk and checking if they're connected.
---

# HotHawk — Adding mailboxes & checking if they're connected

Two jobs:
1. **Add + verify** (interactive) — add one or many sending mailboxes to a workspace, then
   confirm each truly connects, auto-deleting any that don't (the bad-auth / IP-block defense).
2. **Daily health scan** (scheduled) — sweep every HotHawk workspace for disconnected accounts,
   reconnect or remove them, and post a Discord digest. Weekdays only.

---

## Why this skill exists (the IP-block problem)

When you add a mailbox, HotHawk reports `currentConnectionStatus: GATHERING` ("Gathering data")
**regardless of whether the credentials are correct.** A mailbox added with a wrong
password/host sits in GATHERING and silently retries IMAP/SMTP auth against the underlying
**SiteGround** server. SiteGround does not whitelist our domains, so those repeated auth
failures get the **HotHawk sending IP blocklisted** — which has bitten us repeatedly on new adds.

The backend gives us **no "creds are wrong" flag** mid-gather. The rich fields
(`connectionAttempts`, `lastConnectionStatus`, `isConnectedToEmailEngine`) are returned **only**
in the create response, never on list — and the list endpoint exposes only
`currentConnectionStatus` (`INITIALIZING → GATHERING → CONNECTED → DISCONNECTED`).

**So the only safe defense is time-based:** a healthy mailbox reaches `CONNECTED`; a bad-auth one
never does. If a freshly-added mailbox hasn't connected within the threshold, **delete it** to
stop the auth retries before they trigger an IP block, and flag it.

> Calibration (LNP Machining, 2026-06-03): 15 freshly-added healthy mailboxes were still
> `GATHERING` at ~20 min with **0 disconnected**, then climbed to `CONNECTED`. Healthy gather is
> genuinely slow here — so the "stuck = bad auth" threshold must be generous. Default **45 min**.

---

## Talking to HotHawk

- **MCP server (preferred for the interactive add/verify):** tools
  `mailboxes_connect_imap_create`, `mailboxes_list`, `reconnect_mailbox`, `mailboxes_delete`,
  `workspaces_short_list`.
- **REST (used by the daily scan; stdlib, headless):** `https://api.hothawk.ai/v1`,
  `Authorization: Bearer <HOTHAWK_API_TOKEN>`. Confirmed routes:
  `GET /workspaces` · `GET /mailboxes?workspaceId=&page=&take=150` ·
  `POST /mailboxes/connect-imap` · `DELETE /mailboxes/{id}`.
  There is **no REST reconnect route** (only the MCP `reconnect_mailbox` tool); the daily scan
  therefore reconnects by delete + re-add via connect-imap — functionally identical for a dead box.

`HOTHAWK_API_TOKEN` lives in `G:\Shared drives\Capy Outreach\global.env.md` (backtick-wrapped).

### Workspaces — always resolve live
The hardcoded slug→UUID tables in `scripts/block_all.py` / `backfill_hothawk_blocklist.py` /
`hothawk-api/SKILL.md` **disagree with each other** — do not trust them for naming. Resolve names
↔ UUIDs at runtime via `workspaces_short_list` (MCP) or `GET /v1/workspaces`. (12 today: Alpha
Grainger, Franklin Casting, Tech-Max, Shellcast, Megatech, ATW, Capy, HV OpCos, VRC,
Patriot, LNP Machining, General Foundry.)

---

## Credentials reference (gitignored)

`reconnect_mailbox` and re-add both need the IMAP/SMTP creds, and passwords aren't stored in
`global.env`. Keep them in **`G:\Shared drives\Capy Outreach\hothawk-mailbox-creds.json`**
(gitignored). This doubles as the source for the add flow, so you don't paste creds each time:

```json
{
  "domains": {
    "lnp-machining.com": {"host": "mail.lnp-machining.com", "smtpPort": 465, "imapPort": 993,
                          "password": "•••", "usernameStyle": "email"}
  },
  "overrides": {
    "special@domain.com": {"host": "mail.domain.com", "smtpPort": 465, "imapPort": 993,
                          "password": "•••", "username": "special@domain.com"}
  }
}
```
- Per-email `overrides` beat per-domain `domains`. `usernameStyle: "email"` (default) → username =
  full address. Without this file the daily scan still **reports**; it just can't auto-reconnect.

---

## Job 1 — Add mailboxes + verify they connect (interactive, MCP)

1. **Resolve the workspace** (name → UUID via `workspaces_short_list`).
2. **Collect inputs:** the email list, the display name (firstName / lastName, e.g. "Sofia" /
   "Alvarez"), and per-domain SMTP/IMAP host + ports + password. Pull these from the creds file
   when present; otherwise take them from the user. **Username defaults to the full email address.**
   Same host/ports usually apply across a client's domains — only the domain part changes
   (e.g. `mail.lnp-machining.com` / 465 / 993).
3. **Pre-check for duplicates:** `mailboxes_list` with `email=` (or list the workspace once) and
   **skip any address already present** — report which you skipped. (On the LNP run,
   `sofiaa@lnpmachine.com` was already connected.)
4. **Add each new mailbox** with `mailboxes_connect_imap_create`:
   `firstName, lastName, email, workspaceId, imapUsername, imapPassword, imapHost, imapPort,
   smtpUsername, smtpPassword, smtpHost, smtpPort`. A successful create returns
   `currentConnectionStatus: INITIALIZING` and assigns a `connectionId`.
   - **Add in small batches when the password is unverified** — a wrong password spawns many
     simultaneous auth-hammering mailboxes, which is exactly what blocks the IP. Verify one works
     before firing the rest.
5. **Verify with a backoff poll** — `mailboxes_list` filtered by `email`, at roughly **+2m, +5m,
   +10m, +20m, +30m, +45m**:
   - `CONNECTED` → success; stop polling that one.
   - `DISCONNECTED` → bad auth; fail fast (treat as step 6).
   - still `INITIALIZING` / `GATHERING` past **T_max = 45 min** → **likely bad auth**.
6. **On bad auth (timeout or DISCONNECTED): auto-delete + flag.** Call `mailboxes_delete` to stop
   the auth retries (kills the IP-block risk), and post a short Discord note via
   `notify_discord.py` naming the deleted address(es) and the likely cause (wrong password / host).
7. **Report a final table:** each email → `CONNECTED` | `DELETED (bad auth — reason)` |
   `SKIPPED (already present)`.

> Polling spans up to 45 min. Run the waits as background sleeps and re-check, or hand the
> verification to the daily scan if you can't babysit it — but the auto-delete safety only fires
> if something watches the threshold, so prefer to see at least the first mailbox reach CONNECTED.

---

## Job 2 — Daily health scan (scheduled, `scripts/check_mailbox_health.py`)

Plain-Python REST scan (no model tokens). Runs weekdays 08:00 via Task Scheduler.

```
py scripts/check_mailbox_health.py                 # report + Discord digest, NO changes (default)
py scripts/check_mailbox_health.py --remediate     # also reconnect / delete dead accounts
py scripts/check_mailbox_health.py --workspace "LNP Machining"
py scripts/check_mailbox_health.py --no-discord    # print only (local inspection)
py scripts/check_mailbox_health.py --always-notify # post even when all-healthy
```

What it does each run:
1. `GET /workspaces` (live list) → for each, page `GET /mailboxes` and bucket by status (mirrors
   the UI tiles: Total / Connected / Disconnected / Gathering).
2. Tracks per-account state across runs in `scripts/state/health_state.json` (gitignored) so it
   can tell a *just-started* gatherer from one *stuck for hours*.
3. **Remediation (only with `--remediate`):**
   - `DISCONNECTED` + creds on file → **reconnect** (delete + re-add). Recorded so that if it's
     still dead on a later run (after a ~20h grace), it's **deleted + flagged** rather than churned.
   - `DISCONNECTED` + no creds → **flag** for manual handling (never blind-delete).
   - `GATHERING`/`INITIALIZING` longer than `--stuck-hours` (default **6h**) → **flag only**
     (never auto-delete a gatherer — that's the add flow's job, with its own 45-min window).
4. Posts **one Discord digest** — silent unless something is actionable (or `--always-notify`).

**Remediation starts OFF.** `run_health_check.ps1` runs report-only so you can watch a few digests
first; add `--remediate` to its `$argline` when you trust it.

### Schedule it (run once)
```powershell
cd go-capy-outreach\skills\hothawk-mailbox-connect\scripts\scheduled
.\register_scheduler.ps1      # registers HotHawk_MailboxHealth, weekdays 08:00
```
Interactive principal (so the mapped `G:\` drive, token, creds, and Discord webhook resolve),
`StartWhenAvailable`, `MultipleInstances=IgnoreNew`. Logs to `scripts/scheduled/logs/health_*.log`.

---

## Real-time disconnect/reconnect alerts (webhook)

The daily scan is the safety net; the webhook is the **real-time** alert. HotHawk fires native
`MAILBOX_DISCONNECTED` and `MAILBOX_RECONNECTED` events, so we hear about a drop the moment it
happens instead of up to a day later — and a disconnect is the early warning for the IP-block
problem (a bad-cred mailbox, or one whose IP SiteGround blocklisted, falls off).

This routes through the **already-deployed `email-ops-bridge`** edge function (which also handles
HotHawk bounces + blocklist labels) — no new function:
- Path: `…/functions/v1/email-ops-bridge/hothawk/<HOTHAWK_WEBHOOK_SECRET>/mailbox_disconnected`
  (and `/mailbox_reconnected`). Slugs must match `HOTHAWK_EVENTS` in
  `supabase/functions/email-ops-bridge/index.ts`.
- `normalize.ts` digs the mailbox address out of the payload; `handlers.ts`
  `handleMailboxConnectivity` posts a Discord alert (🔌 disconnected / ✅ reconnected). Alerting
  only — remediation stays with this skill's add flow + daily scan.
- Event IDs are global: `MAILBOX_DISCONNECTED` = `87a29f06-…`, `MAILBOX_RECONNECTED` = `e000dad1-…`.

**Register the webhooks (24 = 2 events × 12 workspaces), idempotent:**
```
py scripts/register_mailbox_webhooks.py            # dry run — preview
py scripts/register_mailbox_webhooks.py --apply    # create the missing ones
py scripts/register_mailbox_webhooks.py --list     # show which already exist
```
The bridge must be deployed with the mailbox-event support first
(`supabase functions deploy email-ops-bridge`, or via the Supabase MCP). No new secrets needed —
`HOTHAWK_WEBHOOK_SECRET` + `DISCORD_INBOUND_WEBHOOK_URL` already exist on the function.

---

## Files

- `scripts/check_mailbox_health.py` — the daily cross-workspace scan (scan / reconnect / delete / digest).
- `scripts/register_mailbox_webhooks.py` — register the real-time disconnect/reconnect webhooks (user-run; idempotent).
- `supabase/functions/email-ops-bridge/` — receives the webhooks (mailbox_disconnected / mailbox_reconnected slugs) and posts Discord alerts.
- `scripts/scheduled/run_health_check.ps1` — Task Scheduler launcher (report-only until you add `--remediate`).
- `scripts/scheduled/register_scheduler.ps1` — registers `HotHawk_MailboxHealth` (weekdays 08:00). User-run.
- `scripts/state/health_state.json` — per-account first-seen / reconnect-attempt state (gitignored, auto-created).
- Creds: `G:\Shared drives\Capy Outreach\hothawk-mailbox-creds.json` (gitignored).

## Related skills

- `hothawk-api` — general HotHawk wrapper (inbox, CRM, blocklists, analytics, subsequences).
- `ai-sdr-manager` — orchestrator; routes per-client work and owns the scheduled infrastructure.
- `siteground` — where the IMAP mailboxes are actually provisioned; the destination when the daily
  scan flags an account whose creds are wrong or whose IP got blocklisted.

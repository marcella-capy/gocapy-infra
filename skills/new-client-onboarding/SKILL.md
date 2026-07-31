---
name: new-client-onboarding
description: >
  The standard Go Capy onboarding runbook for cold-email infrastructure. Use
  this skill whenever Marcella says "New client [url]", "onboard this new
  client", "More domains for [client]", "add domains for [client]", or provides
  a new client website alongside cold email / outreach / sending domains. Also
  use for "Batch domains for <clients>" / "More domains for multiple clients" /
  "buy domains for these principals" — the batch mode described at the bottom,
  for buying warmup domains across 10+ existing principals in one pass. This
  is an orchestrator: it runs the existing skills (purchasing-domains-porkbun →
  siteground-email-setup → mailbox-onboarder PlusVibe stages → mailbox-onboarder
  HotHawk stages) interactively in one session, with the user approving every
  paid or write action. Do NOT create new scripts — delegate to the existing
  skills listed in each step.
---

# New Client Onboarding (Standard Runbook)

Run steps 1→4 in one interactive session. **Every purchase and every platform
write is gated on Marcella's explicit approval.** Spawn subagents for parallel
read-only checks as useful; all writes go through the main session.

Trigger phrases: **"New client <url>"** (full run) or
**"More domains for <client>"** (start at Step 1, reuse existing client
config/signature, skip questions already answered by existing setup).

---

## Step 0 — Required inputs (ask EVERY run before starting)

1. **Client domain / website URL**
2. **Restrictions on domain names** — words to avoid, style constraints
3. **BDR name** — for new clients (drives mailbox prefixes); existing clients
   use the Client → BDR table in the `siteground` skill
4. **Client slug** — used for signature file and tags
5. **PlusVibe workspace** — always ask: **Forge OR Machining**
6. **HotHawk workspace** — **every client gets its OWN workspace.** New client →
   create one via `POST /v1/workspaces` `{name, currencyCode:"USD"}`, then
   `PATCH /v1/workspaces/{id}` with `isAiCategorisationEnabled:false`. Existing
   client → their own workspace. (The shared "Bottom Shelf" catch-all was split
   up and deleted 2026-07-31 — do not recreate that pattern. The one exception
   is the Harvey Vogel opcos, who share **HV OpCos** by design.)
7. **How many domains** to target (she picks the final list manually)

Ask all of these up front in one question set. Do not proceed until answered.

---

## Step 1 — Domains (`purchasing-domains-porkbun` skill)

1. Fetch the client homepage; extract the most-used brand/industry keywords.
2. Generate ~10 lookalike `.com` candidates honoring the domain restrictions
   from Step 0 (never a near-copycat of the client's real domain).
3. Check availability via Porkbun API (10s between checks); present available
   domains + prices.
4. **Marcella picks which to buy.** Show total price, get explicit approval.
5. Register approved domains (`cost` in PENNIES, `agreeToTerms: "yes"`,
   10s between registrations; never auto-retry a failed paid call).
6. Immediately point nameservers to `ns1.siteground.net` / `ns2.siteground.net`.
6.5. Register the new domains as HetrixTools blacklist monitors:
   `py go-capy-outreach/skills/domain-health/scripts/add_hetrix_monitor.py <domains...>`
   (see Step 8.5 of the `purchasing-domains-porkbun` skill — this is separate from and
   does not touch `inventory.json`, which is auto-generated elsewhere).
7. **Supabase `workspaces` row** — create/update the client's row with its own
   `hothawk_workspace_id`. `sending_domains` is only load-bearing on a SHARED
   workspace (today just HV OpCos); if this client is an HV opco, seed its
   domains there or replies route to `skip:no-principal`. Confirm with Marcella
   before the Supabase write.

---

## Step 2 — SiteGround mailboxes (`siteground-email-setup` skill)

1. Generate the standard 5-prefix mailbox list per domain (BDR name from
   Step 0 or the lookup table), default password, `mail.<domain>` /
   SMTP 465 / IMAP 993.
2. Output the mailbox list **plus one browser-console JS snippet per domain**
   that Marcella pastes into SiteGround Site Tools to create the accounts.
3. **Marcella runs the snippet(s) in SiteGround.** Wait for her confirmation
   before continuing.
4. Save a CSV of the accounts to
   `G:\Shared drives\Capy Outreach\Cold Email Accounts\<Principal>\`
   (create the principal's folder if it doesn't exist yet) — see Output 3 in
   the `siteground-email-setup` skill for the exact format.

(No MailToaster CSV, no ClickUp handoff — removed 2026-07-17. The
browser-console JS is kept.)

---

## Step 3 — PlusVibe email warmup (`mailbox-onboarder` skill, PlusVibe stages)

1. **Mandatory login verification first:** `check_login.py` with
   `--delay 6 --max-consecutive-timeouts 3`. Never add an unverified mailbox;
   never retry a failed login in a loop (SiteGround IP-block risk).
2. Dedup against the target workspace, then bulk-add all verified mailboxes to
   the PlusVibe workspace chosen in Step 0 (Forge or Machining).
3. Turn **warmup ON** with standard warmup config.
4. Add the warmup tag — **the SAME warmup tag for every email**.
5. **Signature:** draft from the signature master doc
   (https://docs.google.com/document/d/1Gf4wLo9-PmDZtmAr4QSs07cW5Qr-Fkt1yqbkAcCSjj8).
   The first line is the **BDR's literal full name** (e.g. `Olivia Garcia`) —
   NOT the `{{sender_first_name}} {{sender_last_name}}` merge tags. Include
   exactly ONE of these phone numbers: `949-209-9625`, `949-524-5765`,
   `949-820-8005`. **Marcella must approve the signature before it is applied.**
   Once approved, apply it to all mailboxes and save it to
   `gocapy-infra/shared-references/signatures/<client-slug>.md`.

   Example (Workplace Modular Systems, BDR Olivia Garcia):
   ```
   Olivia Garcia
   Workplace Modular Systems
   Custom Modular Workstations & Workbenches
   Tel: 949-209-9625 | Independent Sales Representative
   Made in the USA | 4–6 Week Lead Times
   ```

---

## Step 4 — HotHawk (REST API, not MCP)

Use the **REST API** so this works in headless/cron runs too (the HotHawk MCP
server is only present in interactive sessions). Helper script:
`gocapy-infra/skills/mailbox-onboarder/scripts/connect_hothawk.py` —
`POST /v1/mailboxes/connect-imap`, `HOTHAWK_API_TOKEN` via `capy_env`.

```
py connect_hothawk.py --workspace-id <uuid> --csv <email,password csv>
```
- Use the client's OWN workspace uuid. Resolve it live with
  `GET /v1/workspaces/short` — the API silently accepts a dangling workspaceId
  and the objects become invisible in the UI.
- The script skips mailboxes already present (idempotent) and makes ONE login
  attempt each — never a retry loop (SiteGround IP-block protection).
- Invariant: every PlusVibe mailbox must also exist in HotHawk (PlusVibe ⊆ HotHawk).

## Step 5 — Test send (SMTP smoke test)

PlusVibe has **no ad-hoc test-send API** (only warmup + deliverability seed tests),
so send a real one-shot email directly over the mailbox's SMTP via
`mailbox-onboarder/scripts/test_send.py`:
```
py test_send.py --from <first mailbox> --password 'NewAirton@19642026!' \
   --to marcella@gocapy.com --display-name "<BDR name>" \
   --subject "<Client> test send (please ignore)"
```
Run one send from the first mailbox to Marcella's inbox; confirm arrival (note
inbox vs spam). One send, no retries.

---

## Verification (end of session)

- Porkbun `listAll` shows the new domains with SiteGround nameservers.
- `check_login.py` passed on every mailbox before any platform add.
- PlusVibe (chosen workspace) and HotHawk (chosen workspace) show the same
  mailbox set, warmup ACTIVE, one shared warmup tag.
- Supabase `workspaces` row points at the client's own workspace. For an HV
  opco only: `sending_domains` contains the new domains.
- Signature file exists at `shared-references/signatures/<client-slug>.md`
  and was user-approved.

## Out of scope (removed from the standard per Marcella, 2026-07-17)

- ClickUp tasks / Ericka handoff
- MailToaster CSV
- `hothawk-mailbox-connect` connection-verification step
- `inbox-placement` deliverability step
- Porkbun per-domain "API Access" dashboard toggle

---

## Batch Mode (multi-principal)

For buying warmup domains across **10+ existing principals in one pass** (a
few-times-a-year run, e.g. now). Same Steps 1→5 above, same skills, same
per-write approval requirement — the only difference is the loop is over a
list of principals instead of one client, and approval gates are
**consolidated per stage** instead of per principal so Marcella isn't
interrupted 10+ times. Never skip an approval gate just because it's batched —
consolidate the *prompt*, not the *requirement*.

Trigger phrases: **"Batch domains for <clients>"**, **"More domains for
multiple clients"**.

### Batch Step 0 — Build the run table

For each principal Marcella names:
1. Look up **BDR**, **client slug**, and **PlusVibe workspace** from the
   Client → BDR table in the `siteground` skill and the PlusVibe
   workspace-client mapping
   (`go-capy-outreach/skills/plusvibe-campaign-builder-v2/references/workspace-client-mapping.md`).
2. Look up **HotHawk workspace** live via `GET /v1/workspaces` (never
   hardcoded — hardcoded tables are known to drift).
3. Only ask Marcella for what can't be resolved this way: **which
   principals**, **how many domains per principal**, and any **domain-name
   restrictions**.
4. Assemble one table — `principal | BDR | client slug | PlusVibe workspace |
   HotHawk workspace uuid | domain count | restrictions` — and show it to
   Marcella for **one up-front confirmation** before any API calls. If any
   field can't be resolved for a principal, flag it in the table instead of
   guessing.

These are existing principals, not new clients — each already has its own
workspace, and the Supabase `sending_domains` seed step (Step 1.7 above) applies
only to the shared HV OpCos workspace.

### Batch Step 1 — Domains, all principals

Run Step 1 above per principal, but consolidate the two approval points:
1. Generate + check-availability for every principal first (the existing 10s
   Porkbun rate limit serializes naturally across the whole batch — do not
   parallelize `checkDomain`/`create` calls).
2. Present **one combined availability report**, one `client = ... / new
   domains = ...` block per principal (same format as the standalone skill).
3. **One purchase approval** covering all principals — Marcella replies once
   with her picks across the whole batch. Show the grand total price.
4. Register + point nameservers for all approved domains, one pass, still
   sequential per Porkbun's rate limits.
5. Register every newly approved domain as a HetrixTools blacklist monitor
   (Step 6.5 above) — one batch call across all principals, not per-principal.

**Watch the 24h cap:** Porkbun's successful-registration cap is account-specific
and reported live in each `domain/create` response (`limits.success` —
observed as 50/24h on 2026-07-22, not the 10/24h once assumed). Check the
live response, not a hardcoded number. If the batch's approved-domain count
would exceed the remaining daily allowance, say so explicitly and propose
spanning the run across multiple days — never silently truncate the purchase
list.

### Batch Step 2 — SiteGround mailboxes

Generate the mailbox list per domain per principal (Step 2 above), but output
**one combined set of browser-console JS snippets**, each snippet clearly
labeled by principal and domain. Wait for **one combined confirmation** after
Marcella runs all snippets before moving to Step 3.

### Batch Step 3 — PlusVibe warmup

Per principal (workspace stays principal-specific, never shared): run
`check_login.py`, then bulk-add verified mailboxes to that principal's own
PlusVibe workspace with warmup ON and the shared warmup tag. For signatures,
reuse the existing file at `shared-references/signatures/<client-slug>.md`
for any principal that already has one; only draft + present for approval the
signatures of principals that don't — as **one combined review**, not N
separate ones.

### Batch Step 4 — HotHawk

Run `connect_hothawk.py` once per principal against that principal's own
HotHawk workspace uuid from the Batch Step 0 table. Same idempotency and
single-login-attempt rules as Step 4 above.

### Batch Step 5 — Test send

One test send per principal (first new mailbox → marcella@gocapy.com).
Report all results as **one consolidated pass/fail table** (principal, inbox
vs spam) instead of individual confirmations.

### Batch verification (end of session)

One combined checklist across all principals:
- Porkbun `listAll` shows the new domains per principal with SiteGround
  nameservers.
- `check_login.py` passed on every mailbox before any platform add.
- PlusVibe and HotHawk (each principal's own workspace) show matching
  mailbox sets, warmup ACTIVE, the shared warmup tag.
- Signature file exists and is approved for every principal (pre-existing or
  newly drafted this run).
- Test-send table shows an inbox/spam result for every principal.

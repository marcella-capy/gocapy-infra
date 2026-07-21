---
name: new-client-onboarding
description: >
  The standard Go Capy onboarding runbook for cold-email infrastructure. Use
  this skill whenever Marcella says "New client [url]", "onboard this new
  client", "More domains for [client]", "add domains for [client]", or provides
  a new client website alongside cold email / outreach / sending domains. This
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
6. **HotHawk workspace** — new client → **Bottom Shelf**; existing client →
   their respective HotHawk workspace
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
7. **Bottom Shelf only:** seed the new domains into Supabase
   `workspaces.sending_domains` for the Bottom Shelf workspace — otherwise
   replies route to `skip:no-principal`. Confirm with Marcella before the
   Supabase write.

---

## Step 2 — SiteGround mailboxes (`siteground-email-setup` skill)

1. Generate the standard 5-prefix mailbox list per domain (BDR name from
   Step 0 or the lookup table), default password, `mail.<domain>` /
   SMTP 465 / IMAP 993.
2. Output the mailbox list **plus one browser-console JS snippet per domain**
   that Marcella pastes into SiteGround Site Tools to create the accounts.
3. **Marcella runs the snippet(s) in SiteGround.** Wait for her confirmation
   before continuing.

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
- Bottom Shelf workspace id = `0bb515e2-fb32-4676-83ad-ea72e5e909fe` (new clients);
  existing clients use their own workspace uuid.
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
- Bottom Shelf only: Supabase `workspaces.sending_domains` contains the new
  domains.
- Signature file exists at `shared-references/signatures/<client-slug>.md`
  and was user-approved.

## Out of scope (removed from the standard per Marcella, 2026-07-17)

- ClickUp tasks / Ericka handoff
- MailToaster CSV
- `hothawk-mailbox-connect` connection-verification step
- `inbox-placement` deliverability step
- Porkbun per-domain "API Access" dashboard toggle

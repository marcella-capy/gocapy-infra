# gocapy-infra

Tooling for the one-time infrastructure setup behind onboarding a new Go Capy client:
buy sending domains, create the mailboxes, load them into PlusVibe + HotHawk, warm them
up, and confirm they land in the inbox.

Each step below is a skill under `skills/`. Run them top to bottom for a new client.
Open the skill's `SKILL.md` for the detailed steps.

## Onboarding a new client

**Start with the `new-client-onboarding` skill** — it is the standard runbook
(updated 2026-07-17) and orchestrates the others. Trigger: "New client <url>"
or "More domains for <client>".

1. **purchasing-domains-porkbun** — generate cold-email sending-domain ideas from the
   client's website (honoring per-client naming restrictions), check availability on
   Porkbun, register the ones the user approves, and point the nameservers at SiteGround.
2. **siteground** — generate the standard 5-prefix mailbox list per domain plus a
   browser-console JS snippet per domain that creates the accounts in Site Tools.
3. **mailbox-onboarder (PlusVibe stages)** — verify every mailbox login (IMAP + SMTP),
   bulk-load into the chosen PlusVibe workspace (Forge or Machining), warmup ON, one
   shared warmup tag, user-approved signature.
4. **mailbox-onboarder (HotHawk stages)** — add the same mailboxes to HotHawk via the
   **REST API** (`connect_hothawk.py`, works headless; Bottom Shelf for new clients).
5. **mailbox-onboarder test send** — one SMTP smoke-test email from a mailbox
   (`test_send.py`); PlusVibe has no ad-hoc test-send API.

No longer part of the standard: ClickUp handoff, MailToaster CSV, browser-console JS,
`hothawk-mailbox-connect` verification step, and `inbox-placement` (those skills still
exist for ad-hoc use).

## Maintenance / utilities

- **hothawk-label-automation** — wires the reply labels (Blacklist / Add to CRM /
  Interested) to their actions. Run once per HotHawk **workspace**, not once per client —
  only needed when a brand-new workspace is created.
- **domain-inventory** — read-only reconciliation of every domain across all registrars.
  A few-times-a-year housekeeping check, not part of onboarding.
- **pipedrive-org-dedup** — find and merge duplicate organization records in Pipedrive
  (exact-domain match, dry-run-first, human-approved). A **monthly** maintenance run, not part
  of onboarding.

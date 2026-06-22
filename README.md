# gocapy-infra

Tooling for the one-time infrastructure setup behind onboarding a new Go Capy client:
buy sending domains, create the mailboxes, load them into PlusVibe + HotHawk, warm them
up, and confirm they land in the inbox.

Each step below is a skill under `skills/`. Run them top to bottom for a new client.
Open the skill's `SKILL.md` for the detailed steps.

## Onboarding a new client, in order

1. **purchasing-domains-porkbun** — generate cold-email sending-domain ideas from the
   client's website, check availability on Porkbun, register them, and point the
   nameservers at SiteGround.
2. **siteground** — create the cold-email mailboxes in SiteGround and export the import CSV.
3. **mailbox-onboarder** — the core step: verify every mailbox login (IMAP + SMTP), then
   bulk-load them into PlusVibe **and** HotHawk with the standard settings, signature,
   client tag, and warmup turned on.
4. **hothawk-mailbox-connect** — confirm the mailboxes actually reached CONNECTED in
   HotHawk (and a daily scan that reconnects or removes dead accounts).
5. **inbox-placement** — run this **last, after warmup has had time to progress**: a
   deliverability test that shows whether the accounts land in Inbox vs Spam.

## Maintenance / utilities

- **hothawk-label-automation** — wires the reply labels (Blacklist / Add to CRM /
  Interested) to their actions. Run once per HotHawk **workspace**, not once per client —
  only needed when a brand-new workspace is created.
- **domain-inventory** — read-only reconciliation of every domain across all registrars.
  A few-times-a-year housekeeping check, not part of onboarding.
- **pipedrive-org-dedup** — find and merge duplicate organization records in Pipedrive
  (exact-domain match, dry-run-first, human-approved). A **monthly** maintenance run, not part
  of onboarding.

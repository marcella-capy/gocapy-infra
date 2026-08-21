# Read this first - gocapy-infra

Built 2026-08-20 (backlog item P34-2). Universal rules (who Marcella is, email
staging, Kodie identity) are in ~/.claude/CLAUDE.md; the outreach estate map is in
gocapy-claude-plugin/CLAUDE.md. Keep this file under ~40 lines.

## What this repo is

One-time per-client infrastructure for the Capy agency (see README.md): buy sending
domains (Porkbun), create mailboxes (SiteGround), connect them to PlusVibe/HotHawk,
run warmup, verify inbox placement. 13 skills under `skills/` - onboarding
(new-client-onboarding, mailbox-onboarder, purchasing-domains-porkbun, siteground,
hothawk-mailbox-connect), monitoring (domain-inventory, inbox-placement,
platform-mirror-audit, hothawk-label-automation), calling (call-load-audit,
call-task-scheduler), data hygiene (org-quick-add, pipedrive-org-dedup).

## Traps learned the hard way (memory does not load in this repo - they live here)

- SiteGround: batch logins in a loop trip an IP block. Space logins out; never
  hammer retries.
- Porkbun: domain-registration caps are ACCOUNT-specific - a cap error can mean
  "use the other account", not "retry later".
- HotHawk webhook registration is idempotent - re-registering is safe; skipping
  "already exists" errors is correct.
- HotHawk mailbox connections need the exact IMAP/SMTP format documented in the
  hothawk-mailbox-connect skill - don't improvise the fields.

## Working state

The tree often sits dirty on a feature branch - don't discard changes, don't
commit unasked.

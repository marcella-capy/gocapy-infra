---
name: platform-mirror-audit
description: >
  Audit HotHawk <-> PlusVibe sending-mailbox parity and emit ready-to-upload
  account JSON (one file per HotHawk workspace, one per PlusVibe workspace), with
  passwords pulled from the consolidated onboarding credential CSVs. Use this skill
  whenever the user wants to reconcile mailboxes across the two platforms, asks
  "what emails are missing from PlusVibe / HotHawk", "do HotHawk and PlusVibe
  mirror each other", "which mailboxes need uploading", "mirror audit", "platform
  parity", or wants the upload JSON/credentials for mailboxes that exist on one
  platform but not the other. It NEVER uploads — it only reports the gaps and
  writes the upload-ready files. Meant to be run a few times a year. For the
  domain sending inventory itself use domain-inventory / domain-health; for
  provisioning brand-new mailboxes use mailbox-onboarder.
---

# Platform Mirror Audit

Read-only reconciliation of the two sending platforms. Never uploads.

## The invariant (this is the whole point)
Per `mailbox-onboarder`: **PlusVibe ⊆ HotHawk**. Every PlusVibe sending mailbox
MUST also exist on HotHawk. HotHawk is allowed extra warmup mailboxes that are
intentionally *not* on PlusVibe. So the gaps split into:

1. **PV-not-on-HH → HARD violation.** Must be added to HotHawk. (`out/hh_*.json`)
2. **HH-not-on-PV**, two sub-cases:
   - a **whole domain** absent from PlusVibe → likely a real sender never mirrored;
     surfaced as an upload *candidate* (`out/pv_*.json`).
   - a **partial extra** on an already-mirrored domain → almost always a warmup
     mailbox; reported under "IGNORE", never written to an upload file.

## What it does
1. Rebuilds the domain-health inventory (live HotHawk + PlusVibe mailbox lists),
   unless `--no-rebuild`.
2. Indexes every `email,password` row from the consolidated onboarding credential
   CSVs (`config.json → credential_dirs`, newest file wins).
3. Computes per-domain gaps, **excluding** the HH-only warmup/parking domains in
   `config.json → exclude_domains`.
4. Writes to `out/`:
   - `hh_<workspace>.json` — accounts to add to that HotHawk workspace
     (`POST /v1/mailboxes/connect-imap` body shape).
   - `pv_<workspace>.json` — accounts to add to that PlusVibe workspace
     (bulk-account / consolidated-CSV field shape).
   - `report.md` — the human summary (must-fix / candidates / ignore / needs-password / unassigned).

Host is **always forced** to `mail.<address-domain>` 993/465 (SiteGround canonical),
never the CSV's host — some CSV rows carry a sibling domain's host and fail the
onboarder's domain-match gate.

## Run
```
py scripts/mirror_audit.py               # rebuild inventory + audit + write out/
py scripts/mirror_audit.py --no-rebuild  # reuse existing inventory.json
py scripts/mirror_audit.py --print       # also echo report.md
```

## Then upload (manually — the skill won't)
- HotHawk: `mailbox-onboarder/scripts/connect_hothawk.py` (or the HH MCP
  `mailboxes_connect_imap_create`), one workspace at a time.
- PlusVibe: bulk-account upload into the matching workspace, then tag the new
  accounts with the client tag (see `mailbox-onboarder`).
Verify each password with an IMAP+SMTP login first (`mailbox-onboarder/check_login.py`).

## Files
- `scripts/mirror_audit.py` — the audit.
- `references/config.json` — exclude list, PlusVibe workspace ids, client-tag→PV-workspace
  map (only used to place a whole-domain gap that has no PlusVibe sibling), credential dirs.
- `out/` — generated; **gitignored** because the JSON holds plaintext passwords.

## Notes / gotchas
- `out/` is credential-bearing. Never commit it; never paste its contents anywhere shared.
- Reuses `gocapy-claude-plugin/.../domain-health/scripts` (`dh_common`, `build_inventory`)
  via a cross-repo `sys.path` insert — both marketplace repos must be checked out side by side.
- "needs-password" in the report = a gap mailbox with no row in any credential CSV;
  find/rotate its password before uploading. "unassigned" = target workspace couldn't be
  resolved (add the client tag to `config.json`).

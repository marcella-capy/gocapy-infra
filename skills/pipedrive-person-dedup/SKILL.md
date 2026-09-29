---
name: pipedrive-person-dedup
description: >
  Find and merge duplicate PERSON / contact records in Pipedrive across the whole
  database. A matching name alone never counts - every pair needs the names to agree
  (exact, nickname, initial, abbreviated or compound surname) PLUS a second signal:
  same email, same LinkedIn profile, same organization, same direct phone or same
  company email domain. Proposed merges are sorted into tiers (high / medium /
  review); two DIFFERENT people sharing one email or LinkedIn go to a separate
  bad-data list and are never merged. Use whenever the user wants to "dedup people",
  "merge duplicate contacts / persons", "clean up duplicate people in Pipedrive", or
  asks for the "monthly person dedup". Pipedrive MERGE only, never deletes; writes a
  reviewable CSV first and merges only what the user approved. Never changes a name,
  phone or email. For companies use pipedrive-org-dedup.
---

# Pipedrive Person Dedup (monthly)

Mirror of `pipedrive-org-dedup` for people. **Dry-run CSV -> human approval -> execute.**
Shares `pipedrive-org-dedup/scripts/dedup_common.py` (output folder, plan contract, merge executor,
ClickUp posting). Merge primitive: `pipedrive_create.merge_persons()` (`PUT /persons/{loser}/merge`).

## Matching (Dedupely / Pipedrive / Salesforce-style: name + one more thing)
**Names agree** (required on every pair; `Name` / `names_agree` in `scripts/dedup_persons.py`):
- exact: same first + last after cleanup (accents, ", MBA", Jr/Sr/III/PhD/Dr/PMP dropped).
- fuzzy: nickname pair (`references/nicknames.json`: Bob/Robert, Mike/Michael...), initial
  ("J. Smith"), abbreviated last name ("Lisa S." / "Lisa Sampson"), compound surname ("Cesar Perez
  Morelos" / "Cesar Perez"), prefix (Chris/Christopher), small typo (Jaro-Winkler >= 0.92),
  swapped order ("Fairall Paul").

**Second signal** (at least one):
- same email (any address on the record; role inboxes like purchasing@, rfq@ are ignored).
- same LinkedIn profile.
- same organization.
- same direct phone (a number shared by 3+ different names is a switchboard and is ignored).
- same company email domain (free-mail domains ignored).

| Tier | Rule |
|---|---|
| high | names agree (exact or fuzzy) AND same email or same LinkedIn profile |
| medium | exact name AND same organization (no conflicting LinkedIn) |
| review | fuzzy name + same org/phone; exact name + same phone at a different org; exact name + same email domain only; tied only through a third record; group of 5+; any names in the group disagree |
| bad data | same email or LinkedIn but the names DISAGREE - never merged, separate CSV to fix |

Most bad-data rows are guessed addresses like chris@company.com given to several people, or an
enrichment tool attaching the wrong LinkedIn.

**Survivor:** a full name first (never keep "Lisa S." over "Lisa Sampson" - names are fill-only,
golden rule 10, so they can't be fixed after the merge) -> most engagement (emails + activities +
deals + notes) -> has Person Research -> lowest id.

## Workflow
Same as the org skill, with the Windows Python `C:\Users\marce\AppData\Local\Python\bin\python.exe`:
```
python scripts/dedup_persons.py                           # dry run: review CSV + bad-data CSV + summary
python scripts/dedup_persons.py --post-task 86bc8kj30     # ...and post to ClickUp as Kodie
python scripts/dedup_persons.py --execute --plan <csv> --tiers high --limit 10     # pilot, check in the UI
python scripts/dedup_persons.py --execute --plan <csv> --tiers high --limit 2500   # one day's batch
python scripts/dedup_persons.py --execute --plan <edited csv> --tiers medium,review
```
**Approval:** a ClickUp reply ("merge the safe ones") approves the whole high tier. Medium/review
rows are approved by returning an edited sheet. Never a reaction.

**Budget:** each merge costs roughly 15-25 Pipedrive tokens, so a full backlog (~7,000 rows) is
more than one day's 120k budget. Run the same command daily; the budget brake stops at the 15%
reserve and `dedup/persons/merged_ledger.jsonl` skips losers already merged.

**Repair:** after each merge the survivor is re-read. Text fields that came back comma-joined (job
title, LinkedIn, Email Validation "ok, valid", Person Research, City/State/Country) are restored to
the survivor's pre-merge value. Name, phone and email are never written. Every merge is logged to
`audit_<date>.jsonl` with both records' pre-merge values (a merge cannot be undone).

Outputs: `gocapy-claude-plugin/go-capy-outreach/shared-references/dedup/persons/<YYYYMMDD>/`.

## Tests
`python scripts/test_dedup_match.py` - fixture tests for the person and org name matchers,
LinkedIn slugs and the joined-field repair (no Pipedrive calls).

## Cadence
Runs inside `PipedriveDedup_Monthly` (see pipedrive-org-dedup `scripts/scheduled/`), registered
DISABLED until Marcella turns it on.

## History
- 2026-09-28: built. First dry run: 96,207 people, ~6,700 high / ~120 medium / ~960 review,
  1,334 bad-data pairs.

## Clay-copies plan (2026-09-28)

`scripts/clay_copy_plan.py` builds a separate `--plan` CSV for the ~3,500 copies the Clay
"Find People -> Update Pipedrive" table created 09-22..09-27 (see memory
clay-table1-create-person-duplicate-source-2026-09-28). Marcella's rule: survivor = the OLDEST
record with an email (else the oldest), one survivor per LinkedIn slug, same-company copies ->
tier high, multi-org / name-mismatch -> review, never chained. The general monthly plan would
keep a NEW empty copy in ~460 cases, so run this one FIRST, then rebuild the monthly plan.
    python clay_copy_plan.py [--rows dump.json] [--post-task 86bc8kj30]      # dry run
    python dedup_persons.py --execute --plan clay_copies_plan_<date>.csv --tiers high --limit 10
2026-09-28 dry run: 3,493 merges / 43 review. Approvals only on task 86bc8kj30.

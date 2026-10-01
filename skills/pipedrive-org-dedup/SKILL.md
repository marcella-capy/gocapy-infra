---
name: pipedrive-org-dedup
description: >
  Find and merge duplicate ORGANIZATION records in Pipedrive across the whole
  database. Matches on website domain, LinkedIn company page and cleaned company
  name, sorts every proposed merge into a confidence tier (high / medium / review /
  name-only) and merges only what a human approved. Use this skill whenever the
  user wants to "dedup Pipedrive", "merge duplicate organizations / companies",
  says there are "too many duplicate orgs", wants to "clean up duplicate companies",
  asks for "duplicates by domain / LinkedIn / name", or asks for the "monthly org
  dedup". It uses Pipedrive's MERGE only - never deletes. It ALWAYS writes a
  reviewable CSV first and NEVER merges until the user approves (a ClickUp reply
  for the whole high tier, or an edited sheet for the rest). Parent/subsidiary
  pairs and division records are flagged, not merged. For PERSON duplicates use
  pipedrive-person-dedup; for research-time dedup see org-research-agent-v2 Step 1c.
---

# Pipedrive Org Dedup (monthly)

Whole-database duplicate-organization cleanup. **Dry-run CSV -> human approval -> execute that
CSV.** `--execute` acts ONLY on the reviewed CSV passed via `--plan`, and only on the tiers passed
via `--tiers`. **It MERGES, it never DELETES** (`PUT /organizations/{loser}/merge`).

## What counts as a duplicate (match rules)
Every CSV row names which rules matched (`match_on`):
- **domain** - same normalized website (`seed_resolver.normalize_domain`; subdomains are distinct;
  `references/dedup-domain-blocklist.json` hosts never group).
- **linkedin** - same LinkedIn company page (built-in org field `linkedin`, `/company/<slug>`).
- **name** - same cleaned name (accents off; Inc/LLC/Corp/Co/Ltd/Group/Holdings and "(OH)" tags
  stripped).

Records linked by domain OR LinkedIn form one group. Each loser row gets a **tier**:

| Tier | Rule | Approval |
|---|---|---|
| high | the same name (suffix/punctuation/typo aside) AND domain or LinkedIn matches | one ClickUp reply approves the whole tier |
| medium | one name is the other plus extra words ("Diebold" / "Diebold Nixdorf") AND same website | row by row in the sheet |
| review | names differ (parent/subsidiary, acquired company, wrong LinkedIn), LinkedIn-only with slightly different names (divisions share the parent page), different Holding Co, group of 6+, or tied only through a third record | row by row |
| name-only | same cleaned name, no shared website/LinkedIn | review list; merged only if explicitly approved |

Sharing a brand word is never enough ("Teledyne Reynolds" vs "Teledyne Qioptiq" are sister
divisions). Marcella decided 2026-09-28: **parent and subsidiary stay separate** - they are flagged
for review, never in the high batch.

**Survivor:** non-empty Company Research -> most people -> most activity/deals/emails/notes ->
lowest id.

**Guards (all in `references/`):**
- `dedup-domain-blocklist.json` - generic hosts (gmail, wix, linkedin.com...) never group.
- `dedup-org-exclusions.json` - protected names (Lockheed Martin, Northrop Grumman, Raytheon,
  Safran): any group containing one is skipped entirely.
- `dedup-protected-pairs.json` - id pairs never merged together (Honeywell 4761 / 52944; Airbus
  31226 / Airbus Helicopter 33190, struck by Marcella 2026-06-22). Add a pair every time a reviewer
  strikes a row so it is not proposed again.

## Files
- `scripts/dedup_orgs.py` - the runner (dry run by default).
- `scripts/dedup_common.py` - shared with pipedrive-person-dedup: output folder, LinkedIn/name
  normalization, plan CSV contract, the merge executor (budget brake, audit, joined-field repair),
  ClickUp posting.
- `scripts/dedup_approval.py` - reads Marcella's "merge them" reply, merges the approved sure list.
- `scripts/scheduled/` - `PipedriveDedup_Weekly` (daily run, Monday reports); see Cadence.
- Outputs: `gocapy-claude-plugin/go-capy-outreach/shared-references/dedup/orgs/<YYYYMMDD>/`
  (`org_dedup_review_*.csv`, `org_dedup_summary_*.json`, `org_dedup_report_*.txt`, `audit_*.jsonl`,
  `results_*.json`) and `dedup/orgs/merged_ledger.jsonl`. Git-ignored.

## Prereqs
- `PIPEDRIVE_API_TOKEN` / `PIPEDRIVE_DOMAIN` in `~/.claude/global.env` (loaded by `capy_env`).
- A current snapshot (`python <capy>/go-capy-outreach/scripts/pd_cache.py --status`).
- Windows Python: `C:\Users\marce\AppData\Local\Python\bin\python.exe`.

## Workflow

### 1. Dry run (always first; zero Pipedrive calls)
```
python scripts/dedup_orgs.py                         # writes the review CSV + summary
python scripts/dedup_orgs.py --post-task 86bc8kj30   # ...and posts it to ClickUp as Kodie
```
The summary gives duplicates **per category** (domain / LinkedIn / name) and **per tier**.

### 2. Approval (REQUIRED)
Approval is a ClickUp reply or a returned sheet, never a reaction. Typical replies:
- "merge the safe ones" -> run the `high` tier from the dry-run CSV.
- An edited sheet (rows she doesn't want deleted) + "merge this" -> run the tiers in that file.
When she strikes a row, add the pair to `dedup-protected-pairs.json`.

### 3. Execute
```
python scripts/dedup_orgs.py --execute --plan <csv> --tiers high --limit 5     # pilot, check in the UI
python scripts/dedup_orgs.py --execute --plan <csv> --tiers high
python scripts/dedup_orgs.py --execute --plan <edited csv> --tiers medium,review
```
Per merge: budget brake (keeps Pipedrive's last 15%) -> survivor + loser snapshot to
`audit_<date>.jsonl` -> MERGE -> re-read survivor -> any text field that came back comma-joined
(website, LinkedIn, industry, custom text fields such as ICP) is restored to the survivor's
pre-merge value. **Name and phone are never written** (golden rule 10). Merged losers go to
`merged_ledger.jsonl` and are skipped on re-runs. Ends with a `RESULT:` line; exit 3 if any merge
failed. Cost is roughly 15-25 Pipedrive tokens per merge.

## Cadence
Weekly flow (Marcella 2026-09-30). `scripts/scheduled/register_scheduler.ps1 [-Enable]` registers
`PipedriveDedup_Weekly` (DAILY 06:10, `run_weekly.ps1`) and removes the retired `PipedriveDedup_Monthly`:
- **Mondays:** org + person reports posted to the dedup task (86bc8kj30). Each carries a Pipedrive
  import file (`*_label_import_<date>.csv`: `<Entity> - ID`, `<Entity> - Labels`) that labels every
  **high-tier copy** (never the survivor) `Duplicate – delete`, keeping the record's current labels.
  Posting records the report in `dedup/<orgs|persons>/reports.jsonl`.
- **Marcella** imports the file, deletes copies by hand, then replies "merge them" on the task.
- **Daily:** `dedup_approval.py` finds her reply (after the newest report, merge/go ahead/yes and no
  wait/hold/no), stores it in `dedup/approvals.json`, and runs `--execute --tiers high` on that
  report's plan every night until done (budget brake + ledger; deleted copies skip as inactive).
  One note on start, one on finish. Medium/review tiers are never merged by this path.
- Say "merge them", never "reply to approve": clickup_call.py rejects reply+approve wording.

## History
- 2026-06-22: first run, domain-only. 1,424 merges, 0 failures.
- 2026-09-28: added LinkedIn + name matching, tiers, parent/subsidiary flagging, protected pairs,
  budget brake, audit log, joined-field repair, fixed output folder, monthly report job.

---
name: pipedrive-org-dedup
description: >
  Find and merge duplicate ORGANIZATION records in Pipedrive across the whole
  database, grouping by exact website domain and merging duplicates into one
  survivor. Use this skill whenever the user wants to "dedup Pipedrive", "merge
  duplicate organizations / companies", says there are "too many duplicate orgs",
  wants to "clean up duplicate companies", or asks for the "monthly org dedup".
  Meant as a MONTHLY maintenance run. It uses Pipedrive's MERGE function only — it
  NEVER deletes organizations; the loser's people/deals/activities fold into the
  survivor. It ALWAYS writes a reviewable CSV first and NEVER merges anything until
  the user has reviewed and approved that CSV (--execute acts only on the approved
  file). Exact-domain matches only (acquisitions sharing a domain DO merge); never
  merges on name similarity or across different domains. For person/contact dedup or
  per-filter research-time dedup (org-research-agent-v2 Step 1c) use those instead —
  this is whole-database org dedup.
---

# Pipedrive Org Dedup (monthly)

Whole-database duplicate-organization cleanup. Two phases, **hard review gate:**
**dry-run CSV → human review & approval → execute that CSV.** The skill never merges anything it
derived on its own — `--execute` acts ONLY on the reviewed CSV you pass via `--plan`.

**It MERGES, it never DELETES.** Every action is Pipedrive's merge endpoint
(`PUT /organizations/{loser}/merge`), which moves the loser's people/deals/activities onto the
survivor. There is no delete call anywhere in this skill.

It reuses the merge primitive and detection rules that already exist in the
`gocapy-claude-plugin` marketplace — no new Pipedrive API code lives in this skill:
- `pd_cache.get_orgs()` — the single 429-safe daily snapshot of every org (read path).
- `seed_resolver.normalize_domain()` — bare-domain normalization (strip scheme/www/path/port).
- `pipedrive_create.merge_orgs()` — `PUT /organizations/{loser}/merge {"merge_with_id": survivor}`.

## What counts as a duplicate
Two orgs whose **website resolves to the exact same normalized domain** (e.g. both `ebad.com`).
Survivor is picked deterministically: **non-empty `Company Research` → most linked people →
lowest org id (oldest)**. All other orgs in the group merge into it.

**Conservative by design:**
- Exact-domain groups only. Never name-similarity, never across different domains.
- Orgs with no/blank website are **skipped** (no exact-domain evidence) — reported, not merged.
- Domains in `references/dedup-domain-blocklist.json` (gmail, parked pages, site builders, etc.)
  are skipped so unrelated companies sharing a generic host don't get merged. Add to that file
  whenever a dry run surfaces a bogus group.

## Files
- `scripts/dedup_orgs.py` — the runner (dry-run by default; `--execute` to merge).
- `references/dedup-domain-blocklist.json` — persistent: domains that must never group. Edit as needed.

## Prereqs
- `PIPEDRIVE_API_TOKEN` / `PIPEDRIVE_DOMAIN` in `~/.claude/global.env` (already set; loaded by
  `capy_env` via `pd_cache`).
- A current Pipedrive snapshot. Refresh if stale:
  `python <capy>/go-capy-outreach/scripts/pd_cache.py --status` (then `--refresh` if needed).
- Python `python3.14` (Windows: `C:\Users\marce\AppData\Local\Python\bin\python.exe`).

> The script auto-locates the `gocapy-claude-plugin` marketplace dir by walking up from this skill.
> If that fails, pass `--capy-root <path to the marketplaces dir>`.

---

## Workflow

### Step 1 — Dry run (always first)
```
python scripts/dedup_orgs.py
```
Prints every duplicate-domain group (survivor + reason, and each loser it would merge) and totals,
and writes a **reviewable CSV** `dedup_orgs_dryrun_YYYYMMDD.csv` (one row per planned merge:
`domain, survivor_id, survivor_name, survivor_reason, loser_id, loser_name, loser_people`) plus a
JSON copy. **No writes to Pipedrive.**

### Step 2 — Show the CSV to the user and get approval (REQUIRED)
Always present the CSV for review — the skill must never merge without it. Walk it together;
spot-check that survivors look right and that no group merges two genuinely different companies
(those mean a generic shared host → add it to `references/dedup-domain-blocklist.json` and re-run
Step 1). The user can also delete/edit rows in the CSV to drop merges they don't want — `--execute`
runs exactly the rows that remain. Get explicit approval before merging.

### Step 3 — Execute the approved CSV (after approval)
Pass the reviewed CSV via `--plan`. Cautious first batch, then the rest:
```
python scripts/dedup_orgs.py --execute --plan dedup_orgs_dryrun_YYYYMMDD.csv --limit 10  # verify these 10 in the UI
python scripts/dedup_orgs.py --execute --plan dedup_orgs_dryrun_YYYYMMDD.csv             # then the full run
```
`--execute` re-derives nothing — it merges exactly the loser→survivor rows in the CSV (Pipedrive
MERGE, no deletes). Without `--plan` it refuses to run. Each merge logs
`[org-merge] {loser} -> {survivor} ({domain})`; results are written to
`dedup_orgs_results_YYYYMMDD.json`. Exit code 3 if any merge failed.

## Cadence
Run monthly. Can be scheduled (a `/schedule` cloud agent or an AISDR scheduled task) to produce
the dry-run report monthly and ping for review — but **merging stays human-approved**.

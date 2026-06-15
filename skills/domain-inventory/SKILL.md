---
name: domain-inventory
description: >
  Build a consolidated inventory of every domain Go Capy owns across Porkbun,
  SiteGround, and GoDaddy — grouped by principal (client / Capy), with the
  expiration date, auto-renew status (ON / OFF / unknown), and annual cost for
  each domain, plus per-principal and grand-total spend. Use this skill whenever
  the user wants to audit, list, or reconcile their domains, asks "how many
  domains do we have", "what are we paying for domains", "which domains are
  expiring / not renewing", "pull all my domains", "domain inventory", "domain
  renewal report", "break down domains by client", or wants a CSV of all domains
  with registrar / expiry / cost. Porkbun is pulled live via API; SiteGround and
  GoDaddy have no list API, so the user pastes their dashboard export (or GoDaddy
  via MCP if connected) and the skill normalizes + merges them. Meant to be run a
  few times a year. For BUYING/registering domains use purchasing-domains-porkbun
  instead; this skill is read-only reporting.
---

# Domain Inventory

Read-only. Pulls Porkbun via API, takes SiteGround + GoDaddy from the user (no list
API exists for either), maps every domain to a **principal**, and emits a CSV +
grouped tables with **expiration / renewal status / annual cost**.

Output columns: `domain, principal, registrar, expiration, renewal, cost`.

## Files
- `scripts/domain_inventory.py` — pulls Porkbun, merges the `--extra` rows, resolves principals, writes the CSV + prints the summary.
- `references/principal-overrides.json` — **persistent config**: registrar prices, slug→display names, internal/Capy hints, manual domain→principal overrides, and DROP (let-lapse) assignments. Edit this when things change; the script reads it every run.
- `references/_example_extra.csv` — sample of the normalized SiteGround/GoDaddy input.

## Prereqs
- `PORKBUN_API_KEY` / `PORKBUN_SECRET_KEY` in `~/.claude/global.env` (already set). If missing, the script still runs SiteGround/GoDaddy and warns.
- The live sending-domain→client map at `gocapy-claude-plugin/go-capy-outreach/shared-references/voices/client-domains.json` (the script auto-locates it; pass `--client-domains PATH` to override).

---

## Workflow

### Step 1 — Porkbun (automatic)
The script pulls it: `ping` → `pricing/get` (TLD renewal price) → `domain/listAll`
(paginated). Each domain gets `expireDate`, `autoRenew` (→ ON/OFF), and the TLD
renewal price as cost. **No paid endpoints are ever called.**

### Step 2 — SiteGround (manual paste)
SiteGround has no list API. Ask the user:

> Open SiteGround → **Client Area → Services → Domains** (the "My Domains" list) and paste it here.

Then parse the paste:
- **Keep** rows marked **"Registered at SiteGround"** (these are SiteGround inventory).
- **Drop** rows marked **"External Domain"** — those are registered elsewhere (Porkbun/GoDaddy) and only *hosted* at SiteGround; they'd double-count. (Optionally cross-check each external domain appears in the Porkbun/GoDaddy pull and flag orphans.)
- Extract `domain` + the expiry date (`Mon DD, YYYY`). Note any **Expired / Expiring soon** flags.
- SiteGround exposes **no auto-renew flag and no price** → `renewal=unknown`, leave `cost` blank (the script fills the flat SiteGround rate from `principal-overrides.json`).

### Step 3 — GoDaddy
**If a GoDaddy MCP is connected** (check with ToolSearch for `godaddy`), call its
list-domains tool and read `domain`, `expiration/renewal date`, `renewAuto`.

**Otherwise (no MCP)** ask the user to paste their GoDaddy **Renewals & billing →
Subscriptions** page (or screenshots). Extract `domain`, the renew/expiry date, and
auto-renew status. Watch for **"N of M results"** pagination and an obscured/cut-off
row — capture *all* pages, and tell the user if any rows stayed hidden. GoDaddy shows
no price in that view → leave `cost` blank (script fills the flat GoDaddy rate).
Note: "Free Websites + Marketing" / "GoDaddy Studio" entries are **products, not
domain registrations** — skip them.

### Step 4 — Build the `--extra` CSV
Write the SiteGround + GoDaddy rows the user gave into one CSV:

```
domain,registrar,expiration,renewal,cost
techmax-mfg.com,SiteGround,2027-01-10,unknown,
gocapy.com,GoDaddy,2026-10-20,ON,
```
- `registrar` must be exactly `SiteGround` or `GoDaddy` (drives the default price).
- `expiration` as `YYYY-MM-DD` (the script also accepts `Mon DD, YYYY`).
- `renewal` = `ON` / `OFF` / `unknown`.
- Leave `cost` blank to use the flat rate; fill it (e.g. `$19.99`) only to override.

### Step 5 — Run
```bash
python scripts/domain_inventory.py \
  --extra /path/to/extra.csv \
  --out "$HOME/Downloads/domain_inventory.csv"
```
The script prints three blocks — **CLIENTS**, **DROP / FORMER**, **CAPY** — with
per-principal counts, annual cost, and a `renew=OFF` count, plus the grand total and
any **UNASSIGNED** domains. The CSV is written to `--out`.

### Step 6 — Present + resolve unknowns
- Show the user the **clients table** and the **Capy table** (split per `capy_principals` in the config).
- Call out: expired / expiring domains, principals with `renew=OFF` (will lapse), and the DROP buckets (savings if let go).
- If anything lands in **Unassigned**, ask the user which principal it belongs to, then **add it to `domain_overrides` in `principal-overrides.json`** so the next run is clean.

---

## Maintenance (when re-running months later)
- **New client / new sending domains** → they flow in automatically once added to `client-domains.json`; only un-mapped lookalikes need a `domain_overrides` entry.
- **Price change** → edit `prices` in `principal-overrides.json` (Porkbun is live from the API; SiteGround/GoDaddy are the flat rates there).
- **Dropped / dead client** → set its domains to `"DROP - <reason>"` in `domain_overrides` so they're grouped under DROP, not billed against a client.
- **Capy buckets** → `capy_principals` lists which principal names render in the Capy table vs the clients table.

## Guardrails
- 100% read-only: only `ping`, `pricing/get`, `domain/listAll` on Porkbun; never `create`/`renew`/`updateNs`.
- Cost = **annual renewal price**, not historical amount paid. Porkbun is exact (live); SiteGround/GoDaddy are flat per-domain rates from the config — note this caveat in the report.
- SiteGround/GoDaddy data is only as complete as the paste — if a GoDaddy page was truncated or a SiteGround export partial, say so rather than implying full coverage.

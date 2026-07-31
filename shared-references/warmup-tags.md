# Warmup tags per principal

The **warmup tag** is the shared keyword that lets our mailboxes recognise each other's warmup
mail. It is the same string in both platforms:

- **PlusVibe** — the `warmup_custom_words` column on the mailbox upload CSV.
- **HotHawk** — a workspace-scoped warmup tag: `GET|POST /v1/warmups/tags`,
  body `{workspaceId, value}`. The field is **`value`**, not `name`.

Until 2026-07-31 there was no single record of these — the only source was the
`warmup_custom_words` column scattered across the per-client CSVs in
`G:\Shared drives\Capy Outreach\Cold Email Accounts\<Principal>\`. This file is that record.

## Rules

1. **A principal can carry more than one tag.** The current tag plus older ones still running from
   earlier mailbox batches. Alpha Grainger runs both `swiss-machining` and `machining-parts`.
   Adding a new tag does **not** retire the old one — retire deliberately or old mailboxes stop
   warming with the rest of the pool.
2. **Tags are workspace-scoped and do not travel.** A new HotHawk workspace starts with zero tags.
   The five workspaces created by the Bottom Shelf split (2026-07-31) all started empty — ATW and
   Megatech were still empty hours later. Whenever a workspace is created, seed its tags.
3. **A shared workspace carries every sharing principal's tag.** HV OpCos holds all three.
4. **Every mailbox in a batch gets the SAME tag** (see `mailbox-onboarder`).

## Current tags (verified live 2026-07-31)

| Principal | Workspace | Warmup tag(s) |
|---|---|---|
| Alpha Grainger | Alpha Grainger | `swiss-machining`, `machining-parts` |
| Franklin Casting | Franklin Casting | `precision-castings`, `casting-components` |
| Shellcast | Shellcast | `investment-casting`, `casting-components` |
| ATW / Parmatech / Judson Smith | ATW | `precision-tubing` |
| Megatech | Megatech | `machining-parts` |
| Harvey Vogel | HV OpCos | `stamping-custom` |
| Workplace Modular | HV OpCos | `workbench-components` |
| Seconn Fabrication | HV OpCos | `metal-fabrication` |
| Capy (internal) | Capy | `outsourced-sales`, `capy-sales` |
| LNP Machining | LNP Machining | `machining-parts` |
| TMX / Tech-Max | Tech-Max | `machining-parts` |
| Patriot Forge | Patriot | `forge-forever`, `forged-rings`, `forging-rings`, `casting-components` |
| General Foundry | General Foundry | `casting-components` |
| VRC | VRC | `plastic-wonders`, `plastic-parts` |

## Reading `GET /v1/warmups/tags` — most of what it returns is NOT ours

Patriot lists 1,332 tags, Tech-Max 556, VRC 97. Almost all are noise from the shared warmup
network, not Go Capy tags. When auditing, ignore:

- `#XXXX-XXX` codes (and pairs like `#3FC6-H1Z #4FF2-4KZ`) — warmup-network handshake identifiers.
- Bare email addresses and vendor names (`AltGate`, `AdLib`, `Genwords`, `Referrizer`, `plusvibe`,
  `altgatecapital.com`).
- Random two-word pairs on Tech-Max and VRC (`catalog-scene`, `crater-voting`, `vessel-mutual`, …)
  — other tenants' custom words, visible because the pool is shared.

Ours are the hyphenated **industry** phrases in the table above. Filter with
`^#` and `@` to strip the bulk of the noise, then eyeball the rest.

## Maintaining this

`go-capy-outreach/scripts/migration/ensure_warmup_tags.py` holds the desired set per workspace and
is additive + idempotent:

```
py ensure_warmup_tags.py            # dry run
py ensure_warmup_tags.py --apply
```

Update its `WANT` map and this table together whenever a tag is added or a client is onboarded.

**Open question (unchanged):** no endpoint binds an individual mailbox to a warmup tag — the tag
appears to be purely workspace-level in HotHawk, whereas PlusVibe sets it per mailbox. Confirm
whether HotHawk applies the workspace's tags to every mailbox automatically before assuming a
newly-added mailbox is warming with the right pool.

Related: `mailbox-onboarder` (Step 5), `new-client-onboarding` (Step 3), `siteground` Output 3.

---
name: hothawk-label-automation
description: >
  One-time setup that wires HotHawk thread LABELS to downstream actions (blacklisting, opportunity
  creation, sequence cleanup) via the email-ops-bridge edge function. Run it once per HotHawk workspace
  — at first rollout and again whenever a new client workspace is created. It (1) creates the Go Capy
  "Blacklist" + "Add to CRM" labels that are missing, (2) registers the SPECIFIC_LABEL_ADDED webhooks
  for every action label, and (3) audits HotHawk's automatic AI categorisation so you can turn it OFF
  (labels must be human-applied). Use for "set up HotHawk label automation", "wire HotHawk labels",
  "new HotHawk workspace label setup", or "register the blacklist/interest/RFQ label webhooks".
---

# hothawk-label-automation

Make a teammate's **label** in HotHawk trigger the right action. This is the **one-time setup** side
(labels + webhooks + AI-categorisation off). The runtime logic lives in the plugin repo
(`gocapy-claude-plugin`): the `email-ops-bridge` edge function + the `hothawk-crm-action` skill + the
`crm_action_dispatcher` scheduled task. Run this whenever a **new workspace** is onboarded.

## Prerequisite: AI auto-categorisation must be OFF
The whole point is that **humans** apply labels intentionally. If HotHawk's AI auto-categorisation is on,
it applies reply labels itself and would fire actions on the AI's guesses. The client-settings API has no
toggle for it, so turn it off in each workspace's **HotHawk inbox/AI settings (UI)**. Audit with:
```
py scripts/disable_ai_categorisation.py            # lists labels that carry an AI rule, per workspace
```

## Label -> action contract
Label names are matched case-insensitively by the edge function's `classifyLabel`.

| Label | Action | Where it runs |
|---|---|---|
| **Blacklist Domain All** | global: PlusVibe blocklist (all workspaces) + Pipedrive **Org ICP -> No** + `_global-domains.md` | edge function (in-line) |
| **Blacklis Contact All** *(no "t" — 20-char cap)* | global: PlusVibe blocklist (all workspaces) + Pipedrive **Person ICP -> No** + `_global-contacts.md` | edge function |
| **Blacklist Domain** | per-principal domain blacklist (`<slug>-domains.md`) | edge function |
| **Blacklist Contact** | per-principal contact blacklist (`<slug>-contacts.md`) | edge function |

On a SHARED HotHawk workspace (Bottom Shelf hosts 6 principals) the workspace id can't identify the
principal, so per-principal labels resolve it from the message's from/to/cc **mailbox domains** matched
against `workspaces.sending_domains` (seeded from `voices/client-domains.json`, 2026-07-15). No/ambiguous
match → `skip:no-principal` + a `processing_errors` row (`step='resolve-principal'`) — never a guess.
Unshared workspaces keep the plain workspace-id lookup. New Bottom Shelf principal or new lookalike
domain ⇒ update that workspace row's `sending_domains`.
| **Wrong Person** | per-principal contact blacklist | edge function |
| **Not Interested** | per-principal contact blacklist | edge function |
| **Indication Interest** / **Meeting Request** | create HotHawk opportunity in "Indication of Interest" + mark the company (by email domain) Complete in the principal's PlusVibe sequence | enqueued -> `hothawk-crm-action` skill |
| **RFQ** | move that contact's opportunity to "Doc Received (NDA, RFQ)" | enqueued -> `hothawk-crm-action` skill |
| **Add to CRM** *(label created, action = Phase B)* | read the email, add sender + people mentioned to Pipedrive | not wired yet |
| **Out of Office** *(Phase B)* | same as Add to CRM | not wired yet |
| **Automated Reply** | none (drop) | edge function (skip) |

Blacklist rows land in Supabase `public.blacklist` and are drained into the MD files by the plugin's
`sync_blacklist_to_md.py` (scheduled `AISDR_BlacklistSync`).

## Setup procedure (per new workspace, or all at once)

1. **Create missing labels** (Blacklist set + Add to CRM; reply/CRM labels already exist):
   ```
   py scripts/ensure_labels.py                 # DRY RUN — shows present/missing
   py scripts/ensure_labels.py --apply         # create the missing ones
   py scripts/ensure_labels.py --apply --slug <client_slug>   # one workspace
   ```

2. **Register the label webhooks** (SPECIFIC_LABEL_ADDED -> email-ops-bridge `/label_added`):
   ```
   py scripts/register_webhooks.py             # DRY RUN
   py scripts/register_webhooks.py --list      # which (workspace,label) webhooks already exist
   py scripts/register_webhooks.py --apply [--slug <client_slug>]
   ```
   Idempotent best-effort (skips a label whose webhook already targets our endpoint). HotHawk has no
   unique constraint — always dry-run first.

3. **Turn off AI auto-categorisation** in the workspace UI; confirm with `disable_ai_categorisation.py`.

Active workspaces are read live from Supabase `public.workspaces` (is_active, hothawk_workspace_id set),
so the scripts never drift from a hardcoded list. Credentials (`HOTHAWK_API_TOKEN`, `HOTHAWK_WEBHOOK_SECRET`,
`SUPABASE_PROJECT_REF`, `SUPABASE_SERVICE_ROLE_KEY`) come from `G:\Shared drives\Capy Outreach\global.env.md`.

## Runtime pieces (plugin repo — not run here, listed for context)
- `supabase/functions/email-ops-bridge/` — receives the webhook, classifies the label, does blacklist
  actions in-line and enqueues interest/RFQ jobs. Deploy via Supabase MCP `deploy_edge_function`.
- `supabase/migrations/*_hothawk_crm_jobs.sql` — the interest/RFQ job queue.
- `skills/hothawk-crm-action/` — the agent that creates/moves opportunities + marks the company complete
  in PlusVibe; drained by `crm_action_dispatcher.py` (scheduled task `AISDR_CrmAction`).

## Verification
After setup on one workspace (e.g. tmx), apply each label to a test thread and confirm:
- Blacklist Contact -> a `public.blacklist` row (client_slug=tmx); after the sync, the email is in `tmx-contacts.md`.
- Blacklist Domain All -> the domain is in every PlusVibe workspace blocklist + the org's Pipedrive ICP is No + `_global-domains.md` updated.
- Indication Interest -> a `hothawk_crm_jobs` row; after `crm_action_dispatcher.py` runs, the opportunity exists in "Indication of Interest" and the company's leads show Completed in PlusVibe.
- RFQ -> the opportunity is in "Doc Received (NDA, RFQ)".
- Automated Reply -> `email_events` row status `skipped`, no side effects.
Inspect `email_events` (status processed/skipped/error) and `processing_errors` for any failures.
```
py scripts/register_webhooks.py --list   # confirm all 9 action labels are wired in every workspace
```

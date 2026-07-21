# Client email signatures

One file per client (`<slug>.md`) holding that client's **email signature**, used when
onboarding a sending mailbox (set as the PlusVibe account `signature`).

## Rules
- **Line 1 is always the BDR's literal full name** (e.g. `Juliana Matos`) — NEVER the
  `{{sender_first_name}} {{sender_last_name}}` merge tags (Marcella's rule, re-confirmed 2026-07-20;
  each client has one fixed BDR — see the client → BDR table in the `siteground` skill). Files that
  still start with merge tags are stale: substitute the client's BDR name when applying, and fix the file.
- **Phone:** only **3 numbers are allowed** in any signature (Marcella's live set, 2026-07-17:
  `949-209-9625`, `949-524-5765`, `949-820-8005`), and each client uses exactly one, baked into its
  file. `949-436-6696` is no longer in the allowed set — replace it with `949-524-5765` when touching
  a file that still uses it.
  (The per-persona "Email Assignment" numbers from the source doc are **not** signature phones.)
- Each file contains **only** the signature block (no headers/frontmatter) so it can be read verbatim.
- PlusVibe stores signatures as HTML; the onboarding skill wraps each line as
  `<div><br>line1<br>line2…</div>` at apply time. `warmup_signature` stays OFF.

## Slugs
`tmx`, `lnp-machining`, `harvey-vogel`, `general-foundry`, `vrc`, `shellcast`, `megatech`,
`franklin`, `patriot-forge`, `usai`, `alpha-grainger`, `judson`, `atw` (the last two are
Parmatech sub-brands).

> `usai.md` description was drafted by Claude (gaskets/seals, per the `gasket-seals` warmup tag) and approved by the user 2026-06-04.

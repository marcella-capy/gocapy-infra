# Client email signatures

> **LEGACY (2026-08-21).** These files are PlusVibe signatures, and PlusVibe cold outreach
> winds down ~September 2026 — do not invest new work here. Live signatures are the HTML
> baked into HotHawk campaign steps, and phone numbers are now **per-principal Telnyx
> lines** (one number per client; map lives in the `mailbox-onboarder` skill's Signatures
> section). The phone numbers below are OUTDATED — never copy a number from these files
> into anything that sends.

One file per client (`<slug>.md`) holding that client's **email signature**, used when
onboarding a sending mailbox (set as the PlusVibe account `signature`).

## Rules
- **Line 1 is always the BDR's literal full name** (e.g. `Juliana Matos`) — NEVER the
  `{{sender_first_name}} {{sender_last_name}}` merge tags (Marcella's rule, re-confirmed 2026-07-20;
  each client has one fixed BDR — see the client → BDR table in the `siteground` skill). Files that
  still start with merge tags are stale: substitute the client's BDR name when applying, and fix the file.
- **Phone (OUTDATED — see the legacy note above):** the old rule allowed 3 shared numbers
  (`949-209-9625`, `949-524-5765`, `949-820-8005`). Since 2026-08-21 every principal has its
  own Telnyx line and the shared numbers are personal work lines — they must not appear in
  any newly applied signature.
  (The per-persona "Email Assignment" numbers from the source doc are **not** signature phones.)
- Each file contains **only** the signature block (no headers/frontmatter) so it can be read verbatim.
- PlusVibe stores signatures as HTML; the onboarding skill wraps each line as
  `<div><br>line1<br>line2…</div>` at apply time. `warmup_signature` stays OFF.

## Slugs
`tmx`, `lnp-machining`, `harvey-vogel`, `general-foundry`, `vrc`, `shellcast`, `megatech`,
`franklin`, `patriot-forge`, `usai`, `alpha-grainger`, `judson`, `atw` (the last two are
Parmatech sub-brands).

> `usai.md` description was drafted by Claude (gaskets/seals, per the `gasket-seals` warmup tag) and approved by the user 2026-06-04.

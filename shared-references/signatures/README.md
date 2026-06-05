# Client email signatures

One file per client (`<slug>.md`) holding that client's **email signature**, used when
onboarding a sending mailbox (set as the PlusVibe account `signature`).

## Rules
- **Line 1 is always** `{{sender_first_name}} {{sender_last_name}}` — PlusVibe merge tags so the
  signature renders the actual sender's name. Never hard-code a person's name.
- **Phone:** only **3 numbers are allowed** in any signature, and each client uses exactly one
  (already baked into its file):
  - `949-524-5765` — Tech-Max, General Foundry, Shellcast, Megatech, Judson, AT Wall
  - `949-436-6696` — Harvey Vogel, Franklin, Patriot Forge
  - `949-820-8005` — LNP, VRC, USAI, Alpha Grainger
  (The per-persona "Email Assignment" numbers from the source doc are **not** signature phones.)
- Each file contains **only** the signature block (no headers/frontmatter) so it can be read verbatim.
- PlusVibe stores signatures as HTML; the onboarding skill wraps each line as
  `<div><br>line1<br>line2…</div>` at apply time. `warmup_signature` stays OFF.

## Slugs
`tmx`, `lnp-machining`, `harvey-vogel`, `general-foundry`, `vrc`, `shellcast`, `megatech`,
`franklin`, `patriot-forge`, `usai`, `alpha-grainger`, `judson`, `atw` (the last two are
Parmatech sub-brands).

> `usai.md` description was drafted by Claude (gaskets/seals, per the `gasket-seals` warmup tag) and approved by the user 2026-06-04.

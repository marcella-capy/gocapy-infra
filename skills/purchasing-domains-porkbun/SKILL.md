---
name: purchasing-domains-porkbun
description: Generate cold email sending domain ideas for a client based on their website, check availability via Porkbun, list domains in the account, register new domains, update nameservers to SiteGround, and renew existing ones — all via the Porkbun API v3. Use this skill whenever the user wants to find domain names for cold email outreach, create sending domains, find alternative domains for a client, register a domain at Porkbun, renew a Porkbun domain, list domains in the Porkbun account, update nameservers, or says things like "generate domains for", "find cold email domains for", "what domains should I use for", "buy this domain at Porkbun", "register this domain", "renew my domain", "list my Porkbun domains", "update nameservers", or provides a client website URL and wants domain suggestions. Always use this skill when a client domain or URL is mentioned alongside cold email, outreach, or sending domains, OR when any Porkbun domain operation (check / list / register / renew / nameservers) is requested.
---

# Purchasing Domains (Porkbun)

End-to-end domain workflow on Porkbun: generate cold email domain ideas, check availability, list account inventory, register new domains, point nameservers to SiteGround, and renew existing ones via the Porkbun API v3.

## Context

Sending domains are used for cold email campaigns — they must look legitimate, professional, and closely related to the client's real brand. Clients are typically in **aerospace, defense, nuclear, and medical manufacturing**.

The goal: domains that feel like a natural sibling or slight variation of the real brand — not generic, not spammy, not too long.

---

## Authentication

The Porkbun API uses two keys, stored in the `.env` file at the project root:

- `PORKBUN_API_KEY` — public API key (starts with `pk1_`)
- `PORKBUN_SECRET_KEY` — secret API key (starts with `sk1_`)

Load them before any API call:

```bash
source ~/.claude/global.env
```

Most endpoints take the keys in the **JSON body** as `apikey` / `secretapikey`. The `listAll` endpoint also supports **headers** (`X-API-Key` / `X-Secret-API-Key`). Both styles are shown below — match the style each endpoint expects.

If either env var is missing, **stop and ask the user to set them** before continuing.

```bash
[ -z "$PORKBUN_API_KEY" ]    && echo "MISSING PORKBUN_API_KEY"
[ -z "$PORKBUN_SECRET_KEY" ] && echo "MISSING PORKBUN_SECRET_KEY"
```

Sanity-check creds with the ping endpoint before doing anything else:

```bash
curl -s -X POST "https://api.porkbun.com/api/json/v3/ping" \
  -H "Content-Type: application/json" \
  -d "{\"apikey\":\"$PORKBUN_API_KEY\",\"secretapikey\":\"$PORKBUN_SECRET_KEY\"}"
```

A `"status":"SUCCESS"` with `"credentialsValid":true` confirms keys are valid.

---

## Endpoint Reference

| Operation | Method | Endpoint | Auth Style |
|---|---|---|---|
| Check availability | POST | `/api/json/v3/domain/checkDomain/{domain}` | JSON body |
| List all domains | POST/GET | `/api/json/v3/domain/listAll` | JSON body or Headers |
| Register domain | POST | `/api/json/v3/domain/create/{domain}` | JSON body |
| Renew domain | POST | `/api/json/v3/domain/renew/{domain}` | JSON body |
| Update nameservers | POST | `/api/json/v3/domain/updateNs/{domain}` | JSON body |

Rate limit: `checkDomain` is **1 request per 10 seconds**. Insert `sleep 10` between checks.
Rate limit: `domain/create` is **1 attempt per 10 seconds**. The 24h successful-registration cap is account-specific — the `create` response's `limits.success` field reports the live limit/used/TTL for this account (observed as 50/24h on 2026-07-22; do not assume 10 without checking a live response first).

---

## CRITICAL: Cost field is in PENNIES

The Porkbun API returns prices as dollar strings (e.g. `"11.08"`), but the `cost` field in `domain/create` and `domain/renew` must be in **pennies** (integer cents).

**Conversion:** multiply the price by 100 and round to integer.
- `"11.08"` → `1108`
- `"9.73"` → `973`

**Never pass the raw dollar amount as `cost`** — this will cause silent failures or incorrect charges.

---

## Workflow A — Generate, Check, Purchase, and Configure Cold Email Domains

### Step 1 — Visit the client's website

Use `web_fetch` to load the client's homepage. Read the content carefully.

Look for:
- The company name and how it's written or abbreviated
- **High-frequency keywords** — words that appear repeatedly (signals core identity)
- Industry-specific terms: machining, foundry, engineering, fabrication, casting, milling, precision, parts, etc.
- Short taglines, hero text, nav items, and product/service descriptors

### Step 2 — Identify keywords

Extract 5–8 keywords that best represent the brand. Prioritize:
- Words that appear 3+ times on the page
- Words in headlines, nav items, or the hero section
- Short punchy industry terms (not full sentences)

Note the **original domain structure** — hyphens, abbreviations, compound words — and match that style.

### Step 3 — Generate 10 domain candidates

Use **natural language variation** as the primary strategy. If someone saw the domain, it should feel like a plausible sibling of the real brand.

#### Allowed transformations (mix and match):

| Technique | Example | Notes |
|---|---|---|
| Add `-` hyphen | `tech-maxmfg.com` | Makes compound words more readable |
| Remove `-` hyphen | `genfoundry.com` | Combine hyphenated brands |
| Add `s` (pluralize) | `genfoundryservices.com` | Sounds natural |
| Append homepage keyword | `techmaxmachining.com` | Use a word pulled from the site |
| Append short suffix | `techmaxmfg.com` | `mfg`, `hq`, `co`, `part` — only when natural |
| Prepend `get` or `go` | `gettech-max.com` | Only if it sounds natural |
| Abbreviate brand name | `vrc-eng.com` | Shorten long brand names |
| Expand abbreviation | `vrc-manufacturing.com` | Replace acronym with full keyword |

#### Hard rules:
- `.com` only
- Max ~20 characters before `.com`
- No double hyphens, no numbers unless in original
- Must sound like it could plausibly be the real company
- Avoid generic words disconnected from the brand (`bestmfg.com` ❌)
- Avoid anything that looks spammy or low-quality
- Don't go more than ~6 characters longer than the original domain name

### Step 4 — Check availability via Porkbun API

Loop through the 10 candidates with **a 10-second sleep between calls** (Porkbun rate limit).

```bash
source ~/.claude/global.env

for domain in domain1.com domain2.com domain3.com; do
  echo "=== $domain ==="
  curl -s -X POST "https://api.porkbun.com/api/json/v3/domain/checkDomain/$domain" \
    -H "Content-Type: application/json" \
    -d "{\"apikey\":\"$PORKBUN_API_KEY\",\"secretapikey\":\"$PORKBUN_SECRET_KEY\"}"
  echo ""
  sleep 10
done
```

**Response shape (success):**
```json
{
  "status": "SUCCESS",
  "response": {
    "avail": "yes",
    "price": "11.08",
    "regularPrice": "11.08",
    "premium": "no",
    "additional": {
      "renewal": { "price": "11.08" },
      "transfer": { "price": "11.08" }
    },
    "minDuration": 1
  },
  "limits": { ... }
}
```

A domain is **available** when `response.avail == "yes"`. **Capture `response.price` for each available domain** — needed for the `cost` field at registration time (converted to pennies).

If a single check returns `status: "ERROR"`, log it and continue with the rest. Do not abort the batch.

### Step 5 — Present availability results

Output ONLY the following plain-text format — no tables, no headers, no bullet points, no extra commentary:

```
client = [original domain, e.g. axiscades.com]
new domains = [available domain 1],[available domain 2],[available domain 3],...
```

- List only the **available** domains in the `new domains` line
- Comma-separated, no spaces between domains

Internally, also keep a mapping of `{domain: price_in_pennies}` from the check responses — needed for Step 7.

### Step 6 — Ask before purchasing

After presenting results, **always ask the user which domains to purchase**. Never auto-purchase.

> Which of these would you like me to register at Porkbun? Reply with the domains (comma-separated) or "none".

Wait for explicit confirmation. Treat anything ambiguous as "stop and re-ask."

Show the **total price** before final confirmation.

### Step 7 — Register approved domains

For each approved domain, call the create endpoint with `cost` set to the `price` **converted to pennies** (multiply by 100). `agreeToTerms` is **required** and must be `"yes"`.

```bash
# Example: price was "11.08" → cost = 1108
curl -s -X POST "https://api.porkbun.com/api/json/v3/domain/create/$DOMAIN" \
  -H "Content-Type: application/json" \
  -d "{\"apikey\":\"$PORKBUN_API_KEY\",\"secretapikey\":\"$PORKBUN_SECRET_KEY\",\"cost\":$PRICE_IN_PENNIES,\"agreeToTerms\":\"yes\"}"
```

Add a **10-second sleep between registrations** (rate limit: 1 attempt per 10 seconds).

Optional fields (add when relevant):
- `years` — number of years to register (default: 1)
- `autoRenew` — `"yes"` or `"no"` (default: account default)
- `whoisPrivacy` — `"yes"` or `"no"` (Porkbun gives free WHOIS privacy; default is on)
- `coupon` — coupon code if applicable

**Response (success):** `{ "status": "SUCCESS", "domain": "...", "cost": 1108, "orderId": ... }`

If a registration returns `status: "ERROR"`, capture the message and report it — **do NOT retry automatically** (could double-charge).

### Step 8 — Update nameservers to SiteGround

**Immediately after successful registration**, update each purchased domain's nameservers to SiteGround. This is a required step — all cold email domains use SiteGround hosting.

**Default nameservers (always use these unless the user specifies otherwise):**
- `ns1.siteground.net`
- `ns2.siteground.net`

```bash
for domain in domain1.com domain2.com domain3.com; do
  echo "=== $domain ==="
  curl -s -X POST "https://api.porkbun.com/api/json/v3/domain/updateNs/$domain" \
    -H "Content-Type: application/json" \
    -d "{\"apikey\":\"$PORKBUN_API_KEY\",\"secretapikey\":\"$PORKBUN_SECRET_KEY\",\"ns\":[\"ns1.siteground.net\",\"ns2.siteground.net\"]}"
  echo ""
  sleep 2
done
```

**Response (success):** `{ "status": "SUCCESS" }`

If the update fails, report the error but continue with remaining domains.

### Step 8.5 — Register the new domain(s) for blacklist monitoring

Immediately after nameservers are pointed at SiteGround, register every newly
registered domain as a HetrixTools blacklist monitor so the daily RBL sweep
(`domain_health_daily.py` in the `domain-health` skill) picks it up going forward:

```
py go-capy-outreach/skills/domain-health/scripts/add_hetrix_monitor.py domain1.com domain2.com ...
```

This does **not** touch `shared-references/domain-health/inventory.json` — that file
is fully regenerated by `domain_health_daily.py` from live HotHawk/PlusVibe pulls, and
hand-editing it is wrong (it gets overwritten on the next run anyway). A domain shows
up there automatically once it has a connected mailbox; the HetrixTools monitor is a
separate, independent registration that must happen for every new domain regardless of
mailbox status. Duplicate registrations are reported as `[SKIP]`, not an error — safe
to re-run.

### Step 9 — Confirm purchases and configuration

After all registration and nameserver calls complete, output:

```
purchased = domain1.com,domain2.com
nameservers = ns1.siteground.net, ns2.siteground.net (applied to all)
failed = domain3.com (reason: <error message>)
```

If everything succeeded, omit the `failed` line.

---

## Workflow B — List All Domains in the Account

Use the `listAll` endpoint. Supports both POST (body auth) and GET (header auth).

```bash
curl -s -X POST "https://api.porkbun.com/api/json/v3/domain/listAll" \
  -H "Content-Type: application/json" \
  -d "{\"apikey\":\"$PORKBUN_API_KEY\",\"secretapikey\":\"$PORKBUN_SECRET_KEY\",\"start\":0,\"includeLabels\":\"yes\"}"
```

Optional fields:
- `start` — pagination offset (default 0; chunks of 1000)
- `includeLabels` — set to `"yes"` to include domain labels

For accounts with >1000 domains, paginate by incrementing `start` by 1000.

**Response shape:**
```json
{
  "status": "SUCCESS",
  "domains": [
    { "domain": "example.com", "status": "ACTIVE", "tld": "com", "createDate": "...", "expireDate": "...", "autoRenew": "1", "whoisPrivacy": "1", "notLocal": "0", "labels": [...] }
  ]
}
```

Present results as a clean list with domain + expire date + auto-renew status. Sort by expire date ascending so domains expiring soonest surface first.

---

## Workflow C — Register a Domain (Standalone)

When the user wants to register a specific known domain (no generation step):

1. Run `checkDomain` first to confirm availability and get the current `price`.
2. Show the price to the user and ask for explicit confirmation to purchase.
3. Call `domain/create/{domain}` with `cost` = price **in pennies**, `agreeToTerms` = `"yes"`.
4. Update nameservers to SiteGround (Step 8 from Workflow A).
5. Report success/failure.

---

## Workflow D — Renew a Domain

Use the renew endpoint with the current renewal cost **in pennies**.

1. Get the renewal price first — either from `checkDomain` (`response.additional.renewal.price`) or from the user.
2. Confirm with the user before charging.
3. Call `domain/renew/{domain}`:

```bash
curl -s -X POST "https://api.porkbun.com/api/json/v3/domain/renew/$DOMAIN" \
  -H "Content-Type: application/json" \
  -d "{\"apikey\":\"$PORKBUN_API_KEY\",\"secretapikey\":\"$PORKBUN_SECRET_KEY\",\"cost\":$PRICE_IN_PENNIES}"
```

Optional fields:
- `years` — number of years to renew (default: 1)

**Response (success):** `{ "status": "SUCCESS" }`

If renewal fails, report the error message verbatim — do not retry automatically.

---

## Workflow E — Update Nameservers

When the user wants to change nameservers on an existing domain:

```bash
curl -s -X POST "https://api.porkbun.com/api/json/v3/domain/updateNs/$DOMAIN" \
  -H "Content-Type: application/json" \
  -d "{\"apikey\":\"$PORKBUN_API_KEY\",\"secretapikey\":\"$PORKBUN_SECRET_KEY\",\"ns\":[\"ns1.siteground.net\",\"ns2.siteground.net\"]}"
```

Default to SiteGround nameservers unless the user specifies different ones:
- `ns1.siteground.net`
- `ns2.siteground.net`

**Response (success):** `{ "status": "SUCCESS" }`

---

## Worked Examples

### Example 1
**Input:** `https://www.tech-max.com/`
**Keywords found:** CNC, machine, machining, precision, large parts, shop
**Generated domains:**
- `techmax-mfg.com` — remove hyphen + mfg suffix
- `techmaxmachining.com` — remove hyphen + homepage keyword
- `tech-maxmachine.com` — keep hyphen + keyword variant
- `techmaxmfg.com` — compressed + suffix
- `techmaxmachine.com` — compressed + keyword

### Example 2
**Input:** `https://www.genfoundry.com/`
**Keywords found:** foundry, service, casting, gen
**Generated domains:**
- `genfoundryserv.com` — abbreviated keyword suffix
- `gen-foundry.com` — add hyphen for readability
- `genfoundryservice.com` — full keyword suffix
- `genfoundryservices.com` — pluralized keyword suffix

### Example 3
**Input:** `https://www.vrc-es.com/`
**Keywords found:** manufacturing, engineering, solutions, vrc
**Generated domains:**
- `vrc-manufacturing.com` — expand abbreviation with keyword
- `vrcmanufacturing.com` — no hyphen variant
- `vrc-eng.com` — abbreviate engineering
- `vrc-engineering.com` — full keyword expansion
- `general-foundry.com` — inspired by homepage content

---

## Tips & Guardrails

- When the original has a hyphen, always offer both hyphenated and non-hyphenated variants
- When the brand is an acronym (like `vrc`), expand it using homepage keywords
- When the brand is a long compound word, try abbreviating it
- Prioritize **closeness to the original** over creativity
- If `PORKBUN_API_KEY` or `PORKBUN_SECRET_KEY` is missing, stop and ask — do not proceed
- **Always confirm with the user before purchasing or renewing** — never auto-execute paid actions
- **Cost is in PENNIES** — multiply the dollar price by 100 (e.g. `"11.08"` → `1108`)
- `agreeToTerms: "yes"` is **required** for `domain/create` — registration fails without it
- Honor the 10-second rate limit on `checkDomain` and `domain/create` — back-to-back calls without sleep will get throttled
- **Always update nameservers to SiteGround after purchase** — this is a required post-purchase step
- After registering a new domain, the user must enable **API Access** for that specific domain in the Porkbun dashboard before any DNS API calls will work on it
- If a paid call (create/renew) returns `ERROR`, surface the exact error message and stop — never retry automatically
- Use **Idempotency-Key** header on registration calls when possible to prevent double-charges on network retries

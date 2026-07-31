---
name: siteground-email-setup
description: >
  Generate the standard cold-email mailbox list (5 prefixes per domain) for a
  Go Capy client's SiteGround-hosted sending domains. Use this skill whenever
  the user wants to set up email accounts in SiteGround, create sending
  accounts for cold email domains, or says things like "email setup for
  [company]", "create the emails in SiteGround", "SiteGround email setup",
  "set up accounts for [client]", "create email accounts for these domains",
  "run the siteground skill for [client]", or provides a client name plus a
  list of sending domains. Input is the client name + domains. Output is a
  plain email + password list grouped by domain, one self-executing
  browser-console JS snippet per domain that creates the accounts in SiteGround
  Site Tools, and a CSV of the accounts saved to the shared drive at
  G:\Shared drives\Capy Outreach\Cold Email Accounts\<Principal>\. For display
  name updates, use the siteground-display-names skill instead.
---

# SiteGround Cold Email Account Setup

This skill takes a **client name + list of sending domains** and produces the
**standard mailbox list** (email + password, grouped by domain) plus **one
browser-console JS snippet per domain** that creates the accounts in SiteGround
Site Tools (**Email → Accounts**, one domain at a time via the domain switcher).

> Note: MailToaster CSV generation and the ClickUp/Ericka handoff were removed
> from this workflow (2026-07-17, per Marcella). The **browser-console JS
> automation is kept** — it's how the accounts actually get created.

---

## Input

The user provides:

- **Client name** (e.g., `Shellcast`, `Franklin Casting`)
- **One or more sending domains** (e.g., `shellcastfoundries.com`)
- Optional: a password override (defaults to `NewAirton@19642026!`)

If the user only gives a client name, ask for the domains. If they only give
domains, try to infer the client from the domains and confirm.

---

## Client → BDR Lookup Table

Each client always has the same BDR. Use this table to look up the BDR's full
name from the client name. The BDR's first and last name drive the five standard
email prefixes (see below).

| Client            | BDR Name           |
|-------------------|--------------------|
| Tech-Max          | Stephanie Nunes    |
| General Foundry   | Luciana Reis       |
| VRC               | Larissa Tavares    |
| Megatech          | Carine Oliveira    |
| Harvey Vogel      | Julia Brooks       |
| Shellcast         | Luiza Campos       |
| Franklin Casting  | Camila Andrade     |
| Patriot Forge     | Juliana Matos      |
| L&P Machining     | Sofia Alvarez      |
| Alpha Grainger    | Diana Brunel       |

If the client isn't in the table, ask the user for the BDR's full name before
proceeding, then add the new client + BDR to this table.

---

## Standard Prefix Convention

For each domain, generate **five email prefixes** from the BDR's first and last
name (lowercased, ASCII, no spaces). The pattern is:

1. `first`
2. `first.last`
3. `firstInitial + last`
4. `first + lastInitial`
5. `last + firstInitial`

**Example — Camila Andrade on `franklin-castings.com`:**

- `camila@franklin-castings.com`
- `camila.andrade@franklin-castings.com`
- `candrade@franklin-castings.com`
- `camilaa@franklin-castings.com`
- `andradec@franklin-castings.com`

Apply the same 5 prefixes to **every domain**. So 3 domains × 5 prefixes = 15
accounts total. Deduplicate if any two rules happen to collide.

---

## Password

Default password: **`NewAirton@19642026!`**

Use this for every account unless the user explicitly overrides it. Both SMTP
and IMAP passwords are identical.

---

## Standard Server Settings (for downstream steps)

| Setting   | Value           |
|-----------|-----------------|
| SMTP Host | `mail.<domain>` |
| SMTP Port | `465` (SSL)     |
| IMAP Host | `mail.<domain>` |
| IMAP Port | `993` (SSL)     |

---

## Output 1: Mailbox List (in chat)

Present a summary plus the full list grouped by domain, for example:

> **Summary:** 5 accounts on 1 domain for Shellcast (BDR: Luiza Campos).
> Password for all: `NewAirton@19642026!`
>
> **`shellcastfoundries.com`** — create in Site Tools → Email → Accounts
> (enter the prefix only; SiteGround appends `@domain`):
> - `luiza`
> - `luiza.campos`
> - `lcampos`
> - `luizac`
> - `camposl`

---

## Output 2: One JavaScript Per Domain

After the list, output **one self-executing JS snippet per domain** that the
user pastes into SiteGround's browser console to create the accounts.

### Why one script per domain

SiteGround's domain switcher reloads the whole page, which kills any running
JavaScript. So never combine domains — the user switches domain in Site Tools,
pastes that domain's script, waits for `🎉 Done!`, then moves to the next.

### Instructions to give the user (per domain)

1. Log into [SiteGround Site Tools](https://tools.siteground.com)
2. Use the domain switcher (top-left) to select the domain
3. Go to **Email → Accounts** in the left sidebar
4. Open the console — **Windows/Linux:** `F12` → **Console** tab; **Mac:** `Cmd + Option + J`
5. Paste the script below, press **Enter**, and wait for `🎉 Done!` before switching domains

### Script template (adapt `domain`, `accounts`, password per domain)

```javascript
(async () => {
  const domain = "shellcastfoundries.com";
  const accounts = [
    { prefix: "luiza", password: "NewAirton@19642026!" },
    { prefix: "luiza.campos", password: "NewAirton@19642026!" },
    { prefix: "lcampos", password: "NewAirton@19642026!" },
    { prefix: "luizac", password: "NewAirton@19642026!" },
    { prefix: "camposl", password: "NewAirton@19642026!" },
  ];

  const delay = (ms) => new Promise(r => setTimeout(r, ms));
  const setReactInput = (input, value) => {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    setter.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
  };

  let created = 0, skipped = 0, failed = 0;
  console.log(`\n📧 Creating ${accounts.length} accounts on ${domain}...\n`);

  for (let i = 0; i < accounts.length; i++) {
    const acct = accounts[i];
    console.log(`  [${i + 1}/${accounts.length}] Creating: ${acct.prefix}@${domain}`);
    try {
      const inputs = document.querySelectorAll('input[type="text"], input[type="password"]');
      const accountInput = [...inputs].find(el =>
        el.placeholder?.toLowerCase().includes('account') ||
        el.name?.toLowerCase().includes('account') ||
        el.name?.toLowerCase().includes('user') ||
        el.id?.toLowerCase().includes('account')
      ) || inputs[0];
      const passwordInput = [...inputs].find(el =>
        el.type === 'password' ||
        el.placeholder?.toLowerCase().includes('password') ||
        el.name?.toLowerCase().includes('password')
      );
      if (!accountInput || !passwordInput) { console.error(`    ❌ Could not find input fields`); failed++; continue; }

      setReactInput(accountInput, acct.prefix);
      await delay(300);
      setReactInput(passwordInput, acct.password);
      await delay(300);

      const createBtn = [...document.querySelectorAll('button')].find(b => b.textContent.trim().toUpperCase() === 'CREATE');
      if (createBtn) { createBtn.click(); await delay(1500); }
      else { console.error(`    ❌ Could not find CREATE button`); failed++; continue; }

      const toasts = document.querySelectorAll('[class*="toast"], [class*="Toastify"], [class*="notification"], [class*="alert"], [class*="snack"], [class*="notice"]');
      let handled = false;
      toasts.forEach(t => {
        const txt = t.textContent.toLowerCase();
        if (txt.includes('already exists') || txt.includes('duplicate')) { console.log(`    ⚠️ Already exists — skipped`); skipped++; handled = true; }
        else if (txt.includes('success') || txt.includes('created')) { console.log(`    ✅ Created successfully`); created++; handled = true; }
      });
      if (!handled) { console.log(`    ✅ Assumed created (no error detected)`); created++; }

      if (i < accounts.length - 1) { console.log(`    ⏳ Waiting 3s...`); await delay(3000); }
    } catch (err) { console.error(`    ❌ Error: ${err.message}`); failed++; }
  }
  console.log(`\n🎉 Done! ${domain} — Created: ${created} | Skipped: ${skipped} | Failed: ${failed}`);
})();
```

### Key rules

- **One domain per script** — never combine domains
- **Prefix only** — enter just the prefix; SiteGround auto-appends `@domain`
- **React-compatible** — always use the `setReactInput` helper

---

## Output 3: Save a CSV to the Shared Drive

After presenting Outputs 1 and 2, always save a CSV of the accounts to:

`G:\Shared drives\Capy Outreach\Cold Email Accounts\<Principal>\`

- **Create the principal's folder if it doesn't already exist** (existing
  folders use short codes like `TMX`, `GF`, `FC`, `VRC`, `LNP`, `HV`,
  `Shellcast`, `AG`, `Patriot` — check for an existing folder before making a
  new one; use the full principal name if no existing short code applies).
- **Filename:** `<FOLDER>_<MM.DD.YY>_<BDR first name>.csv` (date = the day the
  mailboxes were created).
- **Columns:** `first_name,last_name,email,password,smtp_host,smtp_port,imap_host,imap_port`
  — one row per mailbox (every prefix × every domain for that principal).
  Do not include PlusVibe/warmup-specific columns (tags, daily_limit, rampup
  settings, etc.) here — this CSV is a record of the mailboxes themselves,
  not a PlusVibe upload file; warmup config is generated separately in the
  `mailbox-onboarder` skill's PlusVibe stage.

This makes the account list persist for later reference (e.g. "where are the
new mailbox credentials?") instead of only existing in chat.

---

Then wait for the user to confirm the accounts were created before any
downstream step (login verification / PlusVibe / HotHawk) runs.

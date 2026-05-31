---
name: siteground-email-setup
description: >
  Create cold email accounts in SiteGround for Go Capy clients AND generate the
  MailToaster-ready import CSV in one pass. Use this skill whenever the user wants
  to set up email accounts in SiteGround, create sending accounts for cold email
  domains, or says things like "email setup for [company]", "create the emails in
  SiteGround", "SiteGround email setup", "set up accounts for [client]", "create
  email accounts for these domains", "run the siteground skill for [client]", or
  provides a client name plus a list of sending domains. Input is the client
  name + domains + ClickUp task ID (no CSV upload required). Output is (1) a CSV
  in the MailToaster import format, (2) one self-executing JavaScript snippet per
  domain that automates account creation in SiteGround's browser UI, and (3) the
  CSV attached to the ClickUp task along with a tagged comment to Ericka Klein.
  Also triggers on "generate the SiteGround script", "give me the JS for email
  setup", or any mention of creating email accounts alongside SiteGround or cold
  email domains. For display name updates, use the siteground-display-names
  skill instead.
---

# SiteGround Cold Email Account Setup

This skill takes a **client name + list of sending domains + ClickUp task ID**
as input and produces three artifacts:

1. A **CSV file** in the MailToaster import format (saved to the outputs folder)
2. One **self-executing JavaScript snippet per domain** (rendered in chat) that
   the user pastes into SiteGround Site Tools to actually create the accounts
3. The **CSV attached to the corresponding ClickUp task**, with a comment
   assigned to Ericka Klein letting her know the file is ready for upload to
   MailToaster

Display name updates are handled by a separate skill (`siteground-display-names`).
This skill only generates the CSV, creates the email accounts, and hands the
CSV off to Ericka via ClickUp.

---

## Input

The user provides:

- **Client name** (e.g., `Shellcast`, `Franklin Casting`)
- **One or more sending domains** (e.g., `shellcastfoundries.com`, `shellcast-foundry.com`)
- **ClickUp task ID** — REQUIRED. The task where the CSV will be attached and
  where the handoff comment to Ericka will be posted. If the user does not
  provide one, ASK for it before generating any output. Example: `86b9b2vv2`.
- Optional: a setup date for the warmup folder label (defaults to today in `MM.DD.YY`)
- Optional: a password override (defaults to `NewAirton@19642026!`)

No CSV upload required. If the user only gives a client name, ask for the
domains. If they only give domains, try to infer the client from the domains
and confirm. **Always ask for the ClickUp task ID if it wasn't provided.**

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
| Harvey Vogel      | Julia Brown        |
| Shellcast         | Luiza Campos       |
| Franklin Casting  | Camila Andrade     |
| Patriot Forge     | Juliana Matos      |
| L&P Machining     | Sofia Alvarez      |

If the client isn't in the table, ask the user for the BDR's full name before
proceeding.

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

**Example — Luiza Campos on `shellcastfoundries.com`:**

- `luiza@shellcastfoundries.com`
- `luiza.campos@shellcastfoundries.com`
- `lcampos@shellcastfoundries.com`
- `luizac@shellcastfoundries.com`
- `camposl@shellcastfoundries.com`

Apply the same 5 prefixes to **every domain**. So 3 domains × 5 prefixes = 15
accounts total. Deduplicate if any two rules happen to collide.

---

## Password

Default password: **`NewAirton@19642026!`**

Use this for every account unless the user explicitly overrides it. Both SMTP
and IMAP passwords are identical.

---

## Output 1: The CSV File

Save a CSV to the current session's outputs folder using the filename pattern:
`<client-slug>-mailtoaster-<MM.DD.YY>.csv`

It must contain **exactly** these columns, in this order:

```
Email,Daily Limit,Sender Name,SMTP Host,SMTP Port,SMTP Password,SMTP SSL (Optional),IMAP Host,IMAP Port,IMAP Password,Warmup Daily Goal,Warmup Daily Increment,Randomized Daily Volume Min,Randomized Daily Volume Max,Folder,CleanupSentFolder
```

**Per-row values:**

| Column                         | Value                                               |
|--------------------------------|-----------------------------------------------------|
| Email                          | `<prefix>@<domain>`                                 |
| Daily Limit                    | `10`                                                |
| Sender Name                    | BDR full name (e.g., `Luiza Campos`)                |
| SMTP Host                      | `mail.<domain>`                                     |
| SMTP Port                      | `465`                                               |
| SMTP Password                  | `NewAirton@19642026!` (or override)                 |
| SMTP SSL (Optional)            | `TRUE`                                              |
| IMAP Host                      | `mail.<domain>`                                     |
| IMAP Port                      | `993`                                               |
| IMAP Password                  | same as SMTP Password                               |
| Warmup Daily Goal              | `25`                                                |
| Warmup Daily Increment         | `3`                                                 |
| Randomized Daily Volume Min    | `5`                                                 |
| Randomized Daily Volume Max    | `15`                                                |
| Folder                         | `<Client> MM.DD.YY` (e.g., `Shellcast 04.10.26`)    |
| CleanupSentFolder              | `TRUE`                                              |

**Reference sample row** (Shellcast, Luiza Campos, setup 04.10.26):

```
luiza@shellcastfoundries.com,10,Luiza Campos,mail.shellcastfoundries.com,465,NewAirton@19642026!,TRUE,mail.shellcastfoundries.com,993,NewAirton@19642026!,25,3,5,15,Shellcast 04.10.26,TRUE
```

Group rows by domain (all 5 prefixes for domain 1, then all 5 for domain 2, etc.)
so the CSV reads cleanly.

**After writing the CSV**, share it with the user using a `computer://` link
before the JS walkthrough.

---

## Output 2: One JavaScript Per Domain

After generating the accounts, **group by domain**. Then output **one separate
JavaScript script per domain**, with beginner-friendly instructions between each
one. Assume the user has never opened the browser console before.

### Why One Script Per Domain

SiteGround's domain switcher reloads the entire page, which kills any running
JavaScript. A single script with `resume()` logic is confusing and error-prone.
Instead, output one standalone script per domain. The user switches domains in
SiteGround, pastes the next script, and repeats.

### Presentation format

Structure the chat output **exactly** like this — a numbered walkthrough with
one script per domain:

> **Summary:** 15 accounts across 3 domains for Shellcast (BDR: Luiza Campos). Password: `NewAirton@19642026!`. CSV saved to outputs folder and attached to ClickUp task `<task-id>`.
>
> [View your MailToaster CSV](computer:///sessions/<session-id>/mnt/outputs/shellcast-mailtoaster-04.10.26.csv)
>
> ---
>
> ### How to run these scripts
>
> You'll paste one script at a time into your browser's developer console. Here's how:
>
> ---
>
> **DOMAIN 1 of 3: `shellcastfoundries.com`** (5 accounts)
>
> 1. Log into [SiteGround Site Tools](https://tools.siteground.com)
> 2. Use the domain switcher dropdown (top-left) to select **shellcastfoundries.com**
> 3. Go to **Email → Accounts** in the left sidebar
> 4. Open the browser console:
>    - **Windows/Linux:** Press `F12`, then click the **Console** tab
>    - **Mac:** Press `Cmd + Option + J`
> 5. You'll see a text area at the bottom of the panel with a blinking cursor — that's where you paste the script
> 6. Copy the entire script below and paste it into the console, then press **Enter**
> 7. Watch the console — you'll see ✅ messages as each account is created (takes ~3 seconds per account)
> 8. Wait until you see the `🎉 Done!` message before moving to the next domain
>
> ```javascript
> (the script for this domain goes here)
> ```
>
> ---
>
> **DOMAIN 2 of 3: `<next-domain>`** (5 accounts)
>
> 1. Switch the domain to **<next-domain>** using the dropdown at the top-left of SiteGround Site Tools (the page will reload)
> 2. Make sure you're on **Email → Accounts** again
> 3. Open the console again (it gets cleared when the page reloads):
>    - **Windows/Linux:** Press `F12`, then click the **Console** tab
>    - **Mac:** Press `Cmd + Option + J`
> 4. Copy the entire script below and paste it into the console, then press **Enter**
> 5. Wait for the `🎉 Done!` message
>
> ```javascript
> (the script for this domain goes here)
> ```
>
> ---
>
> (Repeat for each additional domain...)
>
> ---
>
> **All done!** Once all accounts are created across all domains, come back here and say **"update display names"** and I'll generate the next step for you.

---

### Script Template (One Per Domain)

Generate one of these per domain. Adapt the `domain`, `accounts` array, and
password from the generated prefixes:

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

      if (!accountInput || !passwordInput) {
        console.error(`    ❌ Could not find input fields`);
        failed++;
        continue;
      }

      setReactInput(accountInput, acct.prefix);
      await delay(300);

      setReactInput(passwordInput, acct.password);
      await delay(300);

      const createBtn = [...document.querySelectorAll('button')].find(b =>
        b.textContent.trim().toUpperCase() === 'CREATE'
      );
      if (createBtn) {
        createBtn.click();
        await delay(1500);
      } else {
        console.error(`    ❌ Could not find CREATE button`);
        failed++;
        continue;
      }

      const toasts = document.querySelectorAll('[class*="toast"], [class*="Toastify"], [class*="notification"], [class*="alert"], [class*="snack"], [class*="notice"]');
      let handled = false;
      toasts.forEach(t => {
        const txt = t.textContent.toLowerCase();
        if (txt.includes('already exists') || txt.includes('duplicate')) {
          console.log(`    ⚠️ Already exists — skipped`);
          skipped++;
          handled = true;
        } else if (txt.includes('success') || txt.includes('created')) {
          console.log(`    ✅ Created successfully`);
          created++;
          handled = true;
        }
      });

      if (!handled) {
        console.log(`    ✅ Assumed created (no error detected)`);
        created++;
      }

      // 3-second cooldown between accounts to avoid rate limiting
      if (i < accounts.length - 1) {
        console.log(`    ⏳ Waiting 3s before next account...`);
        await delay(3000);
      }

    } catch (err) {
      console.error(`    ❌ Error: ${err.message}`);
      failed++;
    }
  }

  console.log(`\n🎉 Done! ${domain} — Created: ${created} | Skipped: ${skipped} | Failed: ${failed}`);
})();
```

### Key Rules for Each Script

- **One domain per script** — never combine multiple domains in a single script
- **Prefix only** — enter just the prefix (e.g., `luiza`), not the full email; SiteGround auto-appends `@domain`
- **React-compatible** — always use the `setReactInput` helper

---

## Output 3: ClickUp Handoff (REQUIRED FINAL STEP)

After the CSV is written and the JS walkthrough is shown in chat, the final
step is to **hand the CSV off to Ericka Klein via ClickUp**. This is not
optional — it is the completion gate for this skill.

### Step 3a — Attach the CSV to the ClickUp task

Use `clickup_attach_task_file` to upload the generated CSV to the task the
user provided.

Required parameters:
- `task_id` → the ClickUp task ID supplied by the user (e.g., `86b9b2vv2`)
- `file_name` → the CSV filename (e.g., `shellcast-mailtoaster-04.09.26.csv`)
- `file_data` → base64-encoded contents of the CSV. Generate this by running
  `base64 -w0 <csv-path>` in Bash, then pass the resulting string.

### Step 3b — Post the handoff comment assigned to Ericka Klein

Use `clickup_create_task_comment` with:
- `task_id` → same ClickUp task ID
- `comment_text` → `@Ericka Klein - ready for upload to MailToaster`
- `assignee` → **`94471045`** (Ericka Klein's ClickUp user ID)
- `notify_all` → `true`

**Why the assignee + notify_all combo:** the MCP `clickup_create_task_comment`
tool does NOT render rich-text `@mentions` inline — plain text "@Ericka Klein"
appears as literal text, not a live tag. Assigning the comment to Ericka's
user ID (`94471045`) and setting `notify_all: true` routes the notification to
her directly, which is functionally equivalent to an @mention.

**Ericka Klein's ClickUp user ID is `94471045`** — hardcode this. Do not look
it up each run unless the user says Ericka has changed.

### Step 3c — Confirm in chat

In the final summary message to the user, explicitly confirm:
- CSV attached to task `<task-id>` ✅
- Handoff comment posted and assigned to Ericka Klein ✅

Only then is the skill run complete.

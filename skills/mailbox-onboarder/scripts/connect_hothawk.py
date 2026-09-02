#!/usr/bin/env python3
"""Connect SiteGround mailboxes to a HotHawk workspace via the REST API.

Why REST (not MCP)
------------------
The HotHawk MCP server is only present in interactive sessions; headless/cron
onboarding runs don't have it. This script hits the REST API directly so Step 4
of onboarding works everywhere. Endpoint (discovered + verified 2026-07-17):

    POST https://api.hothawk.ai/v1/mailboxes/connect-imap
    Authorization: Bearer <HOTHAWK_API_TOKEN>
    {
      "firstName": "Sofia", "lastName": "Alvarez",
      "workspaceId": "<uuid>",
      "email": "user@domain.com",
      "imapHost": "mail.domain.com", "imapPort": 993,
      "smtpHost": "mail.domain.com", "smtpPort": 465,
      "imapUsername": "user@domain.com", "imapPassword": "...",
      "smtpUsername": "user@domain.com", "smtpPassword": "..."
    }

THE NAME IS MANDATORY AND CAN ONLY BE SET HERE
----------------------------------------------
`firstName`/`lastName` become the mailbox's HotHawk "Full Name" -- the sender name
a prospect sees next to the address. HotHawk marks them required but will happily
accept the call WITHOUT them and create a permanently nameless mailbox.

That is not recoverable later. Verified against the live API 2026-08-28: there is
no route that can set a name on a mailbox that already exists. PATCH/PUT
/mailboxes/{id} do not exist (404); /mailboxes/{id}/reconnect rejects name fields
(400) and only replaces credentials; connect-imap and imap-bulk called on an
address that already exists upsert the CREDENTIALS and silently ignore the name.
The only remedy for a nameless mailbox is deleting and re-adding it -- which needs
the password and loses the warmup history -- or editing it by hand in the UI.

An earlier version of this script omitted the name entirely, which left 187 of 469
live mailboxes (40%) nameless. Hence: this script REFUSES to create a mailbox it
cannot name.

SiteGround standard: host = mail.<domain>, IMAP 993 / SMTP 465 SSL, username =
full email address. Only the address + password vary.

⚠️ ALWAYS run `check_login.py` FIRST. The connect call performs a real IMAP/SMTP
login; a wrong password returns `535 Incorrect authentication data`, and repeated
wrong-cred attempts from our IP get SiteGround to block it. This script makes ONE
attempt per mailbox and never retries a failed login. A mailbox already present in
the workspace is skipped (idempotent re-runs).

Usage
-----
  # batch from a CSV with 'email' + 'password' (+ 'first_name'/'last_name')
  py connect_hothawk.py --workspace-id <uuid> --csv accounts.csv
  # single
  py connect_hothawk.py --workspace-id <uuid> --email a@b.com --password 'pw'
      --first-name Sofia --last-name Alvarez
  py connect_hothawk.py --workspace-id <uuid> --csv accounts.csv --json

Name resolution, in order: the CSV's first_name/last_name columns (the standard
SiteGround onboarding CSVs already carry them), then --first-name/--last-name,
then the BDR registered for the address's domain in client-domains.json. If none
of those yields a name the mailbox is REFUSED, not silently created nameless.

Auth: HOTHAWK_API_TOKEN via the shared capy_env loader (global.env).
Exit code 0 only if every requested mailbox is connected (or already present).
"""
from __future__ import annotations

import argparse
import csv as csvmod
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://api.hothawk.ai/v1"

# Reuse the shared local-first credential loader from go-capy-outreach/scripts.
_HERE = Path(__file__).resolve()
for _p in _HERE.parents:
    _cand = _p / "gocapy-claude-plugin" / "go-capy-outreach" / "scripts"
    if (_cand / "capy_env.py").exists():
        sys.path.insert(0, str(_cand))
        break
import capy_env  # noqa: E402


def _token() -> str:
    tok = capy_env.get("HOTHAWK_API_TOKEN")
    if not tok:
        sys.exit("ERROR: HOTHAWK_API_TOKEN not found in global.env(.md)")
    return tok


def _req(method: str, path: str, token: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {"message": "unparseable error body"}


def _domain(email: str) -> str:
    return email.split("@", 1)[1].strip().lower()


def _capitalise(name: str) -> str:
    """Upper-case the first letter of each part; never lower-case the remainder.

    "sofia alvarez" -> "Sofia Alvarez", and "McDonald" survives intact.
    """
    return " ".join(w[0].upper() + w[1:] if w and not w[0].isupper() else w
                    for w in (name or "").split())


def _bdr_by_domain() -> dict[str, str]:
    """domain -> BDR full name, from the shared voices/client-domains.json registry."""
    for _p in _HERE.parents:
        cand = (_p / "gocapy-claude-plugin" / "go-capy-outreach" / "shared-references"
                / "voices" / "client-domains.json")
        if cand.exists():
            data = json.loads(cand.read_text(encoding="utf-8"))
            out: dict[str, str] = {}
            for client in data.get("clients", {}).values():
                if not isinstance(client, dict):
                    continue
                bdr = (client.get("bdr") or "").strip()
                if bdr:
                    for dom in client.get("domains", []) or []:
                        out[dom.strip().lower()] = bdr
            return out
    return {}


def _derives(full: str, local: str) -> bool:
    """Does `full` generate `local` under the 5 standard SiteGround prefix forms?"""
    first, _, last = (full or "").strip().partition(" ")
    f = re.sub(r"[^a-z]", "", first.lower())
    l = re.sub(r"[^a-z]", "", last.lower())
    if not f or not l:
        return False
    forms = {f, l, f + l, l + f, f[0] + l, l + f[0], f + l[0], l[0] + f}
    return re.sub(r"[^a-z]", "", local.lower()) in forms


def resolve_name(email: str, first: str, last: str, bdr_map: dict[str, str]) -> tuple[str, str, str]:
    """Return (firstName, lastName, source). Empty firstName means unresolvable."""
    full = " ".join(x for x in ((first or "").strip(), (last or "").strip()) if x)
    source = "csv/flag"
    if not full:
        full = bdr_map.get(_domain(email), "")
        source = "client-domains-bdr"
    full = _capitalise(full)
    if not full:
        return "", "", "none"
    f, _, l = full.partition(" ")
    return f, l, source


def _existing_emails(token: str, workspace_id: str) -> set[str]:
    """Every address already in the workspace, so re-runs never re-attempt a login.

    HotHawk pages on `page`/`take` and IGNORES `limit`/`skip` -- an earlier version
    sent limit/skip, silently got 25 rows back and stopped there, so the guard only
    ever saw the first 25 mailboxes of a workspace. Nine of twelve workspaces are
    bigger than that, so re-running onboarding re-attempted SiteGround logins for
    mailboxes that already existed -- exactly the repeated-bad-login pattern that
    gets our sending IP blocklisted.
    """
    seen: set[str] = set()
    page = 1
    while True:
        code, payload = _req(
            "GET", f"/mailboxes?workspaceId={workspace_id}&page={page}&take=150", token
        )
        if code != 200:
            break
        rows = payload.get("data") if isinstance(payload, dict) else payload
        rows = rows if isinstance(rows, list) else []
        for r in rows:
            em = (r.get("email") or "").strip().lower()
            if em:
                seen.add(em)
        meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
        if not rows or page >= meta.get("pagesCount", 1):
            break
        page += 1
    return seen


def connect_one(token: str, workspace_id: str, email: str, password: str,
                first_name: str, last_name: str) -> dict:
    email = email.strip().lower()
    host = f"mail.{_domain(email)}"
    body = {
        "firstName": first_name, "lastName": last_name,
        "workspaceId": workspace_id,
        "email": email,
        "imapHost": host, "imapPort": 993,
        "smtpHost": host, "smtpPort": 465,
        "imapUsername": email, "imapPassword": password,
        "smtpUsername": email, "smtpPassword": password,
    }
    code, payload = _req("POST", "/mailboxes/connect-imap", token, body)
    ok = code in (200, 201)
    msg = payload.get("message") if isinstance(payload, dict) else str(payload)
    return {"email": email, "ok": ok, "code": code, "message": msg,
            "name": f"{first_name} {last_name}".strip()}


def main() -> int:
    ap = argparse.ArgumentParser(description="Connect SiteGround mailboxes to HotHawk via REST")
    ap.add_argument("--workspace-id", required=True, help="HotHawk workspace UUID — the client's OWN workspace; resolve live via GET /v1/workspaces/short")
    ap.add_argument("--csv", help="CSV with email,password (+ optional first_name,last_name) columns")
    ap.add_argument("--email")
    ap.add_argument("--password")
    ap.add_argument("--first-name", default="", help="BDR first name (single-mailbox mode)")
    ap.add_argument("--last-name", default="", help="BDR last name (single-mailbox mode)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    token = _token()
    bdr_map = _bdr_by_domain()

    # (email, password, first_name, last_name)
    pairs: list[tuple[str, str, str, str]] = []
    if args.csv:
        with open(args.csv, newline="", encoding="utf-8-sig") as fh:
            for row in csvmod.DictReader(fh):
                row = {(k or "").strip().lower(): v for k, v in row.items()}
                em = (row.get("email") or "").strip()
                pw = (row.get("password") or "").strip()
                if em and pw:
                    pairs.append((em, pw, (row.get("first_name") or "").strip(),
                                  (row.get("last_name") or "").strip()))
    elif args.email and args.password:
        pairs.append((args.email, args.password, args.first_name, args.last_name))
    else:
        sys.exit("ERROR: provide --csv or --email/--password")

    # Resolve every name BEFORE connecting anything. A mailbox we cannot name would
    # be nameless forever (no API can set it afterwards), so refuse the whole run
    # rather than create one -- and refuse before any SiteGround login is attempted.
    resolved: list[tuple[str, str, str, str]] = []
    nameless: list[str] = []
    for em, pw, fn, ln in pairs:
        first, last, src = resolve_name(em, fn, ln, bdr_map)
        if not first or not last:
            nameless.append(em)
            continue
        local = em.strip().lower().split("@", 1)[0]
        if not _derives(f"{first} {last}", local):
            print(f"WARNING: '{first} {last}' does not derive the address {em} "
                  f"(source: {src}) -- check the persona is right for this domain.")
        resolved.append((em, pw, first, last))

    if nameless:
        sys.exit("\n".join([
            "ERROR: no full name could be resolved for:",
            *(f"  {e}" for e in nameless),
            "",
            "HotHawk can only set a mailbox's name when it is created -- a nameless",
            "mailbox cannot be fixed afterwards by any API call. Add first_name/",
            "last_name columns to the CSV, pass --first-name/--last-name, or register",
            "the domain's BDR in shared-references/voices/client-domains.json, then",
            "re-run. Nothing was connected.",
        ]))

    existing = _existing_emails(token, args.workspace_id)
    results = []
    for em, pw, first, last in resolved:
        if em.strip().lower() in existing:
            results.append({"email": em.strip().lower(), "ok": True, "code": 0,
                            "message": "already present -- skipped",
                            "name": f"{first} {last}"})
            continue
        results.append(connect_one(token, args.workspace_id, em, pw, first, last))

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            mark = "[OK]  " if r["ok"] else "[FAIL]"
            print(f"{mark} {r['email']} [{r.get('name', '')}] ({r['code']}) {r['message']}")
        passed = sum(1 for r in results if r["ok"])
        print(f"\nSummary: {passed}/{len(results)} connected (or already present)")

    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

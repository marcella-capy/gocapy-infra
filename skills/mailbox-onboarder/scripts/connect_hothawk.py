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
      "workspaceId": "<uuid>",
      "email": "user@domain.com",
      "imapHost": "mail.domain.com", "imapPort": 993,
      "smtpHost": "mail.domain.com", "smtpPort": 465,
      "imapUsername": "user@domain.com", "imapPassword": "...",
      "smtpUsername": "user@domain.com", "smtpPassword": "..."
    }

SiteGround standard: host = mail.<domain>, IMAP 993 / SMTP 465 SSL, username =
full email address. Only the address + password vary.

⚠️ ALWAYS run `check_login.py` FIRST. The connect call performs a real IMAP/SMTP
login; a wrong password returns `535 Incorrect authentication data`, and repeated
wrong-cred attempts from our IP get SiteGround to block it. This script makes ONE
attempt per mailbox and never retries a failed login. A mailbox already present in
the workspace is skipped (idempotent re-runs).

Usage
-----
  # batch from a CSV with 'email' and 'password' columns
  py connect_hothawk.py --workspace-id <uuid> --csv accounts.csv
  # single
  py connect_hothawk.py --workspace-id <uuid> --email a@b.com --password 'pw'
  py connect_hothawk.py --workspace-id <uuid> --csv accounts.csv --json

Auth: HOTHAWK_API_TOKEN via the shared capy_env loader (global.env).
Exit code 0 only if every requested mailbox is connected (or already present).
"""
from __future__ import annotations

import argparse
import csv as csvmod
import json
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


def _existing_emails(token: str, workspace_id: str) -> set[str]:
    seen: set[str] = set()
    skip = 0
    while True:
        code, payload = _req(
            "GET", f"/mailboxes?workspaceId={workspace_id}&limit=100&skip={skip}", token
        )
        if code != 200:
            break
        rows = payload.get("data") or payload.get("mailboxes") or payload if isinstance(payload, list) else payload.get("data", [])
        rows = rows if isinstance(rows, list) else []
        if not rows:
            break
        for r in rows:
            em = (r.get("email") or "").strip().lower()
            if em:
                seen.add(em)
        if len(rows) < 100:
            break
        skip += 100
    return seen


def connect_one(token: str, workspace_id: str, email: str, password: str) -> dict:
    email = email.strip().lower()
    host = f"mail.{_domain(email)}"
    body = {
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
    return {"email": email, "ok": ok, "code": code, "message": msg}


def main() -> int:
    ap = argparse.ArgumentParser(description="Connect SiteGround mailboxes to HotHawk via REST")
    ap.add_argument("--workspace-id", required=True, help="HotHawk workspace UUID (Bottom Shelf = 0bb515e2-fb32-4676-83ad-ea72e5e909fe)")
    ap.add_argument("--csv", help="CSV with email,password columns")
    ap.add_argument("--email")
    ap.add_argument("--password")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    token = _token()

    pairs: list[tuple[str, str]] = []
    if args.csv:
        with open(args.csv, newline="", encoding="utf-8-sig") as fh:
            for row in csvmod.DictReader(fh):
                em = (row.get("email") or "").strip()
                pw = (row.get("password") or "").strip()
                if em and pw:
                    pairs.append((em, pw))
    elif args.email and args.password:
        pairs.append((args.email, args.password))
    else:
        sys.exit("ERROR: provide --csv or --email/--password")

    existing = _existing_emails(token, args.workspace_id)
    results = []
    for em, pw in pairs:
        if em.strip().lower() in existing:
            results.append({"email": em.strip().lower(), "ok": True, "code": 0, "message": "already present — skipped"})
            continue
        results.append(connect_one(token, args.workspace_id, em, pw))

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            mark = "[OK]  " if r["ok"] else "[FAIL]"
            print(f"{mark} {r['email']} ({r['code']}) {r['message']}")
        passed = sum(1 for r in results if r["ok"])
        print(f"\nSummary: {passed}/{len(results)} connected (or already present)")

    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

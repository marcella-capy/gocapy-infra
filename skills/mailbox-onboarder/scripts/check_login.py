#!/usr/bin/env python3
"""SiteGround credential pre-check — log in ONCE to prove a mailbox password.

Why this exists
---------------
Mailboxes were stalling in HotHawk `GATHERING` and the suspected cause is wrong
SiteGround passwords. A bad-cred mailbox silently retries auth against SiteGround,
which doesn't whitelist our sending IPs — repeated wrong-cred attempts can get the
IP blocklisted. So before a mailbox is added to PlusVibe/HotHawk, we verify the
password with a SINGLE real login (IMAP + SMTP over SSL). No retries, ever.

SiteGround standard (cold-email domains): host = mail.<domain>, IMAP 993 / SMTP 465
SSL, username = the full email address.

Usage
-----
  # single mailbox
  py check_login.py --email eva@techmaxmfg.com --password 'NewArixona19612025!'
  py check_login.py --email x@y.com --password 'p' --host mail.y.com   # override host

  # batch from a CSV that has 'email' and 'password' columns (e.g. the consolidated
  # mailbox CSV). Optional 'imap_host'/'smtp_host' columns are honored if present.
  py check_login.py --csv "C:\\path\\TMX_05.26.26_Consolidated.csv"
  py check_login.py --csv accounts.csv --json        # machine-readable summary

Exit code: 0 only if EVERY checked mailbox passed both IMAP and SMTP; else 1.
Each mailbox is attempted exactly once per protocol. Stdlib only.
"""
from __future__ import annotations

import argparse
import csv as csvmod
import imaplib
import json
import smtplib
import ssl
import sys
from typing import Optional

DEFAULT_IMAP_PORT = 993
DEFAULT_SMTP_PORT = 465
DEFAULT_TIMEOUT = 20

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


def host_for(email: str, override: Optional[str]) -> str:
    if override:
        return override
    domain = email.split("@", 1)[1] if "@" in email else email
    return f"mail.{domain}"


def check_imap(host: str, port: int, email: str, password: str, timeout: int) -> tuple[bool, str]:
    """One IMAP SSL login attempt. Returns (ok, detail)."""
    try:
        ctx = ssl.create_default_context()
        conn = imaplib.IMAP4_SSL(host, port, ssl_context=ctx, timeout=timeout)
        try:
            conn.login(email, password)
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        return True, "ok"
    except imaplib.IMAP4.error as e:
        return False, f"auth rejected: {str(e).strip()[:160]}"
    except (OSError, ssl.SSLError) as e:
        return False, f"connection error: {e.__class__.__name__}: {str(e)[:160]}"
    except Exception as e:  # noqa: BLE001 - report anything cleanly, never raise
        return False, f"error: {e.__class__.__name__}: {str(e)[:160]}"


def check_smtp(host: str, port: int, email: str, password: str, timeout: int) -> tuple[bool, str]:
    """One SMTP SSL login attempt. Returns (ok, detail)."""
    try:
        ctx = ssl.create_default_context()
        server = smtplib.SMTP_SSL(host, port, timeout=timeout, context=ctx)
        try:
            server.login(email, password)
        finally:
            try:
                server.quit()
            except Exception:
                pass
        return True, "ok"
    except smtplib.SMTPAuthenticationError as e:
        return False, f"auth rejected: {str(e)[:160]}"
    except (smtplib.SMTPException, OSError, ssl.SSLError) as e:
        return False, f"connection error: {e.__class__.__name__}: {str(e)[:160]}"
    except Exception as e:  # noqa: BLE001
        return False, f"error: {e.__class__.__name__}: {str(e)[:160]}"


def check_one(email: str, password: str, imap_host: str, smtp_host: str,
              imap_port: int, smtp_port: int, timeout: int) -> dict:
    imap_ok, imap_detail = check_imap(imap_host, imap_port, email, password, timeout)
    smtp_ok, smtp_detail = check_smtp(smtp_host, smtp_port, email, password, timeout)
    return {
        "email": email,
        "imap": {"ok": imap_ok, "host": imap_host, "port": imap_port, "detail": imap_detail},
        "smtp": {"ok": smtp_ok, "host": smtp_host, "port": smtp_port, "detail": smtp_detail},
        "pass": imap_ok and smtp_ok,
    }


def rows_from_csv(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csvmod.DictReader(f)
        cols = {c.lower().strip(): c for c in (reader.fieldnames or [])}
        if "email" not in cols or "password" not in cols:
            sys.exit(f"error: CSV must have 'email' and 'password' columns; found {reader.fieldnames}")
        out = []
        for r in reader:
            email = (r.get(cols["email"]) or "").strip()
            password = (r.get(cols.get("password", "")) or "").strip()
            if not email or not password:
                continue
            imap_host = (r.get(cols.get("imap_host", "")) or "").strip() or None
            smtp_host = (r.get(cols.get("smtp_host", "")) or "").strip() or None
            out.append({"email": email, "password": password,
                        "imap_host": imap_host, "smtp_host": smtp_host})
        return out


def main() -> int:
    ap = argparse.ArgumentParser(description="SiteGround mailbox credential pre-check (one login attempt, IMAP+SMTP).")
    ap.add_argument("--email")
    ap.add_argument("--password")
    ap.add_argument("--host", help="override both IMAP/SMTP host (default mail.<domain>)")
    ap.add_argument("--csv", help="batch: CSV with email,password[,imap_host,smtp_host] columns")
    ap.add_argument("--imap-port", type=int, default=DEFAULT_IMAP_PORT)
    ap.add_argument("--smtp-port", type=int, default=DEFAULT_SMTP_PORT)
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    ap.add_argument("--json", action="store_true", help="emit JSON results")
    args = ap.parse_args()

    if args.csv:
        rows = rows_from_csv(args.csv)
        if not rows:
            sys.exit("error: no usable rows (need non-empty email & password)")
    elif args.email and args.password:
        rows = [{"email": args.email, "password": args.password,
                 "imap_host": None, "smtp_host": None}]
    else:
        sys.exit("error: provide --email and --password, or --csv")

    results = []
    for r in rows:
        imap_host = r["imap_host"] or host_for(r["email"], args.host)
        smtp_host = r["smtp_host"] or host_for(r["email"], args.host)
        results.append(check_one(r["email"], r["password"], imap_host, smtp_host,
                                 args.imap_port, args.smtp_port, args.timeout))

    passed = [x for x in results if x["pass"]]
    failed = [x for x in results if not x["pass"]]

    if args.json:
        print(json.dumps({"results": results,
                          "summary": {"total": len(results),
                                      "passed": len(passed),
                                      "failed": len(failed)}}, indent=2))
    else:
        for x in results:
            mark = "PASS" if x["pass"] else "FAIL"
            print(f"[{mark}] {x['email']}")
            for proto in ("imap", "smtp"):
                p = x[proto]
                line = f"   {proto.upper()}: {'PASS' if p['ok'] else 'FAIL'} ({p['host']}:{p['port']})"
                if not p["ok"]:
                    line += f" — {p['detail']}"
                print(line)
        print(f"\nSummary: {len(passed)}/{len(results)} passed"
              + (f"; FAILED: {', '.join(x['email'] for x in failed)}" if failed else ""))

    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())

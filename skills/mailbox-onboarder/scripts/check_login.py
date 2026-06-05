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

Domain-match gate
-----------------
A bad host (not just a bad password) is a real failure mode here: the onboarding CSV
sometimes lists an imap_host/smtp_host that points at the WRONG domain (e.g. all
"Stephanie" rows list imap_host=mail.techmaxmfg.com regardless of the address's own
domain). That mismatched host is what stalls mailboxes in HotHawk GATHERING. So if a
protocol fails on the provided host AND that host differs from the email's own domain
(mail.<domain>), this script retries that protocol ONCE on the domain host and, if it
passes, reports the corrected host. The address's own-domain host is authoritative;
treat the CSV's host columns as untrusted. Each (host, protocol) pair is still tried at
most once — no same-host retry loop. Disable with --no-domain-fallback.

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
import time
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
              imap_port: int, smtp_port: int, timeout: int,
              domain_fallback: bool = True) -> dict:
    """Verify a mailbox. If a protocol fails on the given host AND that host
    differs from the email's own domain (mail.<domain>), retry ONCE on the
    domain host. This is the domain-match gate: a CSV-provided imap_host/smtp_host
    that points at the wrong domain (the cause of HotHawk GATHERING stalls) is
    caught and corrected to the address's own server. Each (host, protocol) pair
    is still attempted at most once — no same-host retry loop (IP-block safe)."""
    dom = host_for(email, None)  # mail.<email-domain>

    imap_ok, imap_detail = check_imap(imap_host, imap_port, email, password, timeout)
    imap_corrected = False
    if not imap_ok and domain_fallback and imap_host != dom:
        ok2, det2 = check_imap(dom, imap_port, email, password, timeout)
        if ok2:
            imap_ok, imap_detail, imap_host, imap_corrected = True, det2, dom, True

    smtp_ok, smtp_detail = check_smtp(smtp_host, smtp_port, email, password, timeout)
    smtp_corrected = False
    if not smtp_ok and domain_fallback and smtp_host != dom:
        ok2, det2 = check_smtp(dom, smtp_port, email, password, timeout)
        if ok2:
            smtp_ok, smtp_detail, smtp_host, smtp_corrected = True, det2, dom, True

    return {
        "email": email,
        "imap": {"ok": imap_ok, "host": imap_host, "port": imap_port,
                 "detail": imap_detail, "corrected_to_domain": imap_corrected},
        "smtp": {"ok": smtp_ok, "host": smtp_host, "port": smtp_port,
                 "detail": smtp_detail, "corrected_to_domain": smtp_corrected},
        "pass": imap_ok and smtp_ok,
        "host_corrected": imap_corrected or smtp_corrected,
        "verified_host": dom if (imap_corrected or smtp_corrected) else None,
    }


def is_connection_error(result: dict) -> bool:
    """True if a FAIL was a connection error/timeout (NOT 'auth rejected').

    A connection error is the signature of a SiteGround IP block (too many failed
    logins → the host stops responding). It is NOT proof of a bad mailbox — unlike
    an 'auth rejected', which means the server answered and refused the credentials.
    Used to auto-abort a batch before the block deepens.
    """
    for proto in ("imap", "smtp"):
        p = result[proto]
        if not p["ok"] and "auth rejected" not in (p["detail"] or ""):
            return True
    return False


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
    ap.add_argument("--no-domain-fallback", action="store_true",
                    help="disable the domain-match gate (do NOT retry a failed host on mail.<domain>)")
    ap.add_argument("--delay", type=float, default=0.0,
                    help="seconds to sleep between mailboxes (throttle to stay under SiteGround's "
                         "brute-force limit on big batches; e.g. 6)")
    ap.add_argument("--max-consecutive-timeouts", type=int, default=0,
                    help="abort the batch after N consecutive connection-error/timeout mailboxes "
                         "(0=off). A run of timeouts means the SiteGround IP block has tripped — stop "
                         "rather than dig in deeper.")
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
    aborted = False
    consec_conn_err = 0
    for i, r in enumerate(rows):
        if i and args.delay:
            time.sleep(args.delay)
        imap_host = r["imap_host"] or host_for(r["email"], args.host)
        smtp_host = r["smtp_host"] or host_for(r["email"], args.host)
        res = check_one(r["email"], r["password"], imap_host, smtp_host,
                        args.imap_port, args.smtp_port, args.timeout,
                        domain_fallback=not args.no_domain_fallback)
        results.append(res)

        consec_conn_err = consec_conn_err + 1 if is_connection_error(res) else 0
        if args.max_consecutive_timeouts and consec_conn_err >= args.max_consecutive_timeouts:
            aborted = True
            sys.stderr.write(
                f"\n⛔ ABORTED after {consec_conn_err} consecutive connection errors at "
                f"'{res['email']}' — the SiteGround IP block has likely tripped. "
                f"Checked {len(results)}/{len(rows)}; the rest are UNVERIFIED (not failed). "
                f"Wait for the block to clear, then resume with --delay.\n")
            break

    checked = len(results)
    passed = [x for x in results if x["pass"]]
    failed = [x for x in results if not x["pass"]]
    unverified = [r["email"] for r in rows[checked:]]

    if args.json:
        print(json.dumps({"results": results,
                          "summary": {"total": len(rows), "checked": checked,
                                      "passed": len(passed), "failed": len(failed),
                                      "aborted": aborted, "unverified": unverified}}, indent=2))
    else:
        corrected = [x for x in results if x.get("host_corrected")]
        for x in results:
            mark = "PASS" if x["pass"] else "FAIL"
            print(f"[{mark}] {x['email']}")
            for proto in ("imap", "smtp"):
                p = x[proto]
                line = f"   {proto.upper()}: {'PASS' if p['ok'] else 'FAIL'} ({p['host']}:{p['port']})"
                if p.get("corrected_to_domain"):
                    line += "  ⚠️ host corrected to mail.<domain> (provided host was wrong)"
                if not p["ok"]:
                    line += f" — {p['detail']}"
                print(line)
        print(f"\nSummary: {len(passed)}/{checked} passed (of {len(rows)} requested)"
              + (f"; FAILED: {', '.join(x['email'] for x in failed)}" if failed else ""))
        if corrected:
            print("\n⚠️ DOMAIN-MATCH GATE corrected the host for "
                  f"{len(corrected)} mailbox(es) — the provided host was wrong; "
                  "use mail.<domain> when adding to PlusVibe/HotHawk:")
            for x in corrected:
                print(f"    {x['email']} → {x['verified_host']}")
        if aborted:
            print(f"\n⛔ ABORTED on a suspected SiteGround IP block — {len(unverified)} mailbox(es) "
                  "UNVERIFIED (not failed). Resume later with --delay once the block clears.")

    # exit 2 = batch aborted on block; 1 = a real failure; 0 = all checked passed
    return 2 if aborted else (1 if failed else 0)


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Send a real test email FROM a SiteGround sending mailbox (SMTP, one shot).

Why this exists / why not PlusVibe
----------------------------------
PlusVibe's API has NO ad-hoc "send a test email from this mailbox" endpoint — it
only exposes warmup and email-placement (deliverability seed-list) tests. To prove
a freshly-onboarded mailbox can actually deliver outbound mail, send directly over
the mailbox's own SMTP (mail.<domain>:465 SSL) — the same credentials PlusVibe and
HotHawk use. This is the onboarding smoke-test: sender authenticates + a message
lands in a real inbox.

Run this AFTER check_login.py passes. One send, no retries.

Usage
-----
  py test_send.py --from olivia@modularbench.com --password 'pw' --to marcella@gocapy.com
  py test_send.py --from a@b.com --password 'pw' --to you@x.com --subject "Test" --body "hi"

Stdlib only. Exit 0 on a successful send.
"""
from __future__ import annotations

import argparse
import smtplib
import ssl
import sys
from email.message import EmailMessage

DEFAULT_PORT = 465


def domain(email: str) -> str:
    return email.split("@", 1)[1].strip().lower()


def main() -> int:
    ap = argparse.ArgumentParser(description="Send a one-shot SMTP test email from a mailbox")
    ap.add_argument("--from", dest="sender", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--to", dest="recipient", required=True)
    ap.add_argument("--host", help="override SMTP host (default mail.<domain>)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--subject", default="Capy onboarding test — please ignore")
    ap.add_argument("--body", default=None)
    ap.add_argument("--display-name", default=None, help="From display name")
    args = ap.parse_args()

    host = args.host or f"mail.{domain(args.sender)}"
    body = args.body or (
        f"This is an automated onboarding test send from {args.sender}.\n"
        f"If you received this, the mailbox can authenticate and deliver outbound mail.\n"
        f"No action needed."
    )

    msg = EmailMessage()
    from_hdr = f"{args.display_name} <{args.sender}>" if args.display_name else args.sender
    msg["From"] = from_hdr
    msg["To"] = args.recipient
    msg["Subject"] = args.subject
    msg.set_content(body)

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, args.port, timeout=30, context=ctx) as s:
            s.login(args.sender, args.password)
            s.send_message(msg)
    except Exception as e:  # noqa: BLE001 — surface any SMTP/auth error verbatim
        print(f"[FAIL] {args.sender} -> {args.recipient} via {host}:{args.port}: {e}")
        return 1

    print(f"[SENT] {args.sender} -> {args.recipient} via {host}:{args.port}  subject={args.subject!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

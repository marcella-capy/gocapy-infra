#!/usr/bin/env python3
"""Read-only audit: do HotHawk mailbox display names match the email address?

HotHawk stores the display name as `fullName` on each mailbox (GET /mailboxes).
A mailbox minted from one BDR persona should have a fullName whose first/last name
derives the email local-part via the standard prefix forms (first / last /
first.last / last.first / initial combos, either ordering). This flags any mailbox
whose display name does NOT derive its address.

Report-only: HotHawk exposes no API to UPDATE fullName (set at connect-imap time;
otherwise UI / SiteGround). So this prints what's wrong; it never changes anything.

Usage:
  py audit_display_names.py                       # all workspaces
  py audit_display_names.py --workspace "Alpha Grainger"
  py audit_display_names.py --email ericka@agmi-mfg.com   # one address, any workspace
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.hothawk.ai/v1"
ENV_PATH = Path(r"G:\Shared drives\Capy Outreach\global.env.md")

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


def get_token() -> str:
    if not ENV_PATH.exists():
        sys.exit(f"error: global env not found at {ENV_PATH}")
    txt = ENV_PATH.read_text(encoding="utf-8")
    m = re.search(r"HOTHAWK_API_TOKEN[^`]*`([^`]+)`", txt)
    if not m:
        sys.exit("error: HOTHAWK_API_TOKEN not found in env file")
    return m.group(1)


def _request(method: str, path: str, token: str):
    headers = {"Authorization": f"Bearer {token}"}
    req = urllib.request.Request(f"{API}{path}", method=method, headers=headers)
    for attempt, backoff in enumerate((2, 8, 32)):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read().decode("utf-8")
                return r.status, (json.loads(raw) if raw.strip() else None)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(backoff)
                continue
            return e.code, None
        except urllib.error.URLError:
            if attempt < 2:
                time.sleep(backoff)
                continue
            return -1, None
    return -1, None


def list_workspaces(token: str) -> list[dict]:
    status, payload = _request("GET", "/workspaces?page=1&take=200", token)
    if status != 200 or not isinstance(payload, list):
        sys.exit(f"error: GET /workspaces returned {status}")
    return [{"id": w["id"], "name": w.get("name", "?")} for w in payload]


def list_mailboxes(token: str, ws_id: str) -> list[dict]:
    out, page = [], 1
    while True:
        status, payload = _request(
            "GET", f"/mailboxes?workspaceId={ws_id}&page={page}&take=150", token
        )
        if status != 200 or not payload:
            break
        out.extend(payload.get("data", []))
        meta = payload.get("meta", {})
        if page >= meta.get("pagesCount", 1):
            break
        page += 1
    return out


def norm(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(c for c in s.lower() if c.isalnum())


def prefixes(first: str, last: str) -> set:
    """Local-part forms that legitimately derive from a (first, last) name."""
    f, l = norm(first), norm(last)
    out = set()
    if f:
        out.add(f)
    if l:
        out.add(l)
    if f and l:
        out.update({f + l, l + f, f[0] + l, l + f[0], f + l[0], l[0] + f})
    return out


def main():
    p = argparse.ArgumentParser(description="Audit HotHawk display names vs email")
    p.add_argument("--workspace", help="Limit to one workspace name")
    p.add_argument("--email", help="Limit to one email address (any workspace)")
    args = p.parse_args()

    token = get_token()
    workspaces = list_workspaces(token)
    if args.workspace:
        workspaces = [w for w in workspaces if w["name"].lower() == args.workspace.lower()]
        if not workspaces:
            sys.exit(f"error: workspace {args.workspace!r} not found")

    totals = {"OK": 0, "MISMATCH": 0, "NO_NAME": 0}
    for ws in sorted(workspaces, key=lambda w: w["name"]):
        mbs = list_mailboxes(token, ws["id"])
        if args.email:
            mbs = [m for m in mbs if (m.get("email") or "").lower() == args.email.lower()]
        flagged = []
        ok = 0
        for mb in mbs:
            email = (mb.get("email") or "").strip()
            full = (mb.get("fullName") or "").strip()
            local = email.split("@", 1)[0] if "@" in email else email
            first, _, last = full.partition(" ")
            if not full:
                flagged.append((email, full, "NO_NAME"))
                totals["NO_NAME"] += 1
            elif norm(local) in prefixes(first, last):
                ok += 1
                totals["OK"] += 1
            else:
                flagged.append((email, full, "MISMATCH"))
                totals["MISMATCH"] += 1
        if not mbs:
            continue
        print(f"\n=== {ws['name']}  (OK={ok}"
              f"{' MISMATCH=' + str(sum(1 for f in flagged if f[2]=='MISMATCH')) if any(f[2]=='MISMATCH' for f in flagged) else ''}"
              f"{' NO_NAME=' + str(sum(1 for f in flagged if f[2]=='NO_NAME')) if any(f[2]=='NO_NAME' for f in flagged) else ''}) ===")
        for email, full, verdict in sorted(flagged):
            print(f"  [{verdict:<9}] {email:<42} fullName={full!r}")

    print("\n--- TOTALS ---")
    for k in ("MISMATCH", "NO_NAME", "OK"):
        print(f"  {k:<9} {totals[k]}")


if __name__ == "__main__":
    sys.exit(main())

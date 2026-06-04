#!/usr/bin/env python3
"""Register HotHawk MAILBOX_DISCONNECTED + MAILBOX_RECONNECTED webhooks in every workspace.

These fire the moment a mailbox drops (or recovers), so we hear about it in real time instead of
waiting for the next daily scan — a disconnect is the early warning for the SiteGround IP-block
problem. Each webhook POSTs to the already-deployed `email-ops-bridge` edge function, which turns
the payload into a Discord alert (see supabase/functions/email-ops-bridge, handler
handleMailboxConnectivity + the mailbox_disconnected / mailbox_reconnected event slugs).

Idempotent: skips a (workspace, event) pair whose webhook already points at our endpoint.

Destination:  {SUPA}/functions/v1/email-ops-bridge/hothawk/<HOTHAWK_WEBHOOK_SECRET>/<slug>
Event IDs are global across workspaces (resolved live from GET /events to stay correct if they
ever change). Reads HOTHAWK_API_TOKEN + HOTHAWK_WEBHOOK_SECRET from the shared env file.

Usage:
    py register_mailbox_webhooks.py            # dry run — show what WOULD be created
    py register_mailbox_webhooks.py --apply    # actually create the missing webhooks
    py register_mailbox_webhooks.py --apply --workspace "LNP Machining"
    py register_mailbox_webhooks.py --list      # just list existing mailbox webhooks

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.hothawk.ai/v1"
ENV_PATH = Path(r"G:\Shared drives\Capy Outreach\global.env.md")
SUPABASE_FN = "https://qedvkecwtooqjoxkgrme.supabase.co/functions/v1/email-ops-bridge"

# event TYPE -> the URL slug the edge function dispatches on (must match HOTHAWK_EVENTS in index.ts)
WANTED = {
    "MAILBOX_DISCONNECTED": "mailbox_disconnected",
    "MAILBOX_RECONNECTED": "mailbox_reconnected",
}

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


def env_value(key: str) -> str:
    if not ENV_PATH.exists():
        sys.exit(f"error: global env not found at {ENV_PATH}")
    txt = ENV_PATH.read_text(encoding="utf-8")
    m = re.search(rf"{key}[^`]*`([^`]+)`", txt)
    if not m:
        sys.exit(f"error: {key} not found in env file")
    return m.group(1)


def req(method: str, path: str, token: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {token}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(f"{API}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:300]
    except urllib.error.URLError as e:
        return -1, str(e)


def resolve_event_ids(token: str, ws_id: str) -> dict[str, str]:
    status, payload = req("GET", f"/events?workspaceId={ws_id}&category=SYSTEM", token)
    if status != 200 or not isinstance(payload, list):
        sys.exit(f"error: GET /events returned {status}")
    ids = {e["type"]: e["id"] for e in payload if e.get("type") in WANTED}
    missing = set(WANTED) - set(ids)
    if missing:
        sys.exit(f"error: event types not found: {missing}")
    return ids


def main() -> int:
    ap = argparse.ArgumentParser(description="Register HotHawk mailbox connectivity webhooks.")
    ap.add_argument("--apply", action="store_true", help="create missing webhooks (default: dry run)")
    ap.add_argument("--workspace", help="limit to one workspace by name")
    ap.add_argument("--list", action="store_true", help="just list existing mailbox webhooks")
    args = ap.parse_args()

    token = env_value("HOTHAWK_API_TOKEN")
    secret = env_value("HOTHAWK_WEBHOOK_SECRET")

    status, workspaces = req("GET", "/workspaces?page=1&take=200", token)
    if status != 200 or not isinstance(workspaces, list):
        sys.exit(f"error: GET /workspaces returned {status}")
    if args.workspace:
        workspaces = [w for w in workspaces if w.get("name", "").lower() == args.workspace.lower()]
        if not workspaces:
            sys.exit(f"error: no workspace named '{args.workspace}'")

    event_ids = resolve_event_ids(token, workspaces[0]["id"])
    created = existing = failed = 0

    for ws in workspaces:
        ws_id, ws_name = ws["id"], ws.get("name", "?")
        status, hooks = req("GET", f"/webhooks?workspaceId={ws_id}", token)
        hooks = hooks if isinstance(hooks, list) else []
        have = {h.get("destinationUrl") for h in hooks}

        for evt_type, slug in WANTED.items():
            dest = f"{SUPABASE_FN}/hothawk/{secret}/{slug}"
            if args.list:
                mark = "✓" if dest in have else "—"
                print(f"{mark} {ws_name:18} {slug}")
                continue
            if dest in have:
                existing += 1
                continue
            if not args.apply:
                print(f"WOULD create  {ws_name:18} {slug}")
                continue
            body = {
                "name": f"mailbox-health: {evt_type.replace('_', ' ').title()}",
                "destinationUrl": dest,
                "workspaceId": ws_id,
                "eventId": event_ids[evt_type],
            }
            code, resp = req("POST", "/webhooks", token, body)
            if code in (200, 201):
                created += 1
                print(f"created       {ws_name:18} {slug}")
            else:
                failed += 1
                print(f"FAILED {code}   {ws_name:18} {slug}  {resp}")
            time.sleep(0.15)

    if not args.list:
        verb = "created" if args.apply else "would create"
        print(f"\n{verb}={created}  already-present={existing}  failed={failed}  "
              f"workspaces={len(workspaces)}")
        if not args.apply and created == 0:
            print("(dry run — re-run with --apply to create the webhooks shown above)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

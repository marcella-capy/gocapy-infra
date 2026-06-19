#!/usr/bin/env python3
"""Register the Go Capy label->action webhooks in every active HotHawk workspace.

For each Phase-A action label we register a SPECIFIC_LABEL_ADDED webhook (filtered by that workspace's
own label id, so ONLY these labels reach the bridge) pointing at the already-deployed email-ops-bridge
edge function's label_added endpoint. The edge function's classifyLabel decides the action by name.

Labels wired here (the action lives in supabase/functions/email-ops-bridge + the hothawk-crm-action skill):
    Blacklist Contact / Domain          -> per-principal blacklist
    Blacklis Contact All / Domain All   -> global blacklist + PlusVibe blocklist + Pipedrive ICP No
    Wrong Person / Not Interested       -> per-principal contact blacklist
    Indication Interest / Meeting Request -> create opportunity + mark company complete (enqueued)
    RFQ                                 -> move opportunity to "Doc Received (NDA, RFQ)" (enqueued)
    Add to CRM / Out of Office          -> read thread, enrich + add/update Pipedrive person (enqueued add_to_crm)
NOT wired: Automated Reply (drop / no action).

Destination (same secret + endpoint as the existing blocklist/draft webhooks):
    {SUPA}/functions/v1/email-ops-bridge/hothawk/<HOTHAWK_WEBHOOK_SECRET>/label_added

Idempotent best-effort: skips a (workspace, label) pair whose webhook already targets our endpoint with
the same labelId. HotHawk has no unique constraint, so always dry-run first and eyeball the plan.

Usage:
    py register_webhooks.py            # DRY RUN across all active workspaces
    py register_webhooks.py --apply    # create missing webhooks
    py register_webhooks.py --apply --slug general-foundry   # one workspace
    py register_webhooks.py --list     # show which (workspace,label) webhooks already exist
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.hothawk.ai/v1"
ENV_PATH = Path(r"G:\Shared drives\Capy Outreach\global.env.md")
# Global SPECIFIC_LABEL_ADDED event id (resolved live below; this is the confirmed fallback).
SPECIFIC_LABEL_ADDED = "d37fdf5a-56bd-4fd3-b74b-90bfcf491ae7"

WANT = ["Blacklist Contact", "Blacklist Domain", "Blacklis Contact All", "Blacklist Domain All",
        "Wrong Person", "Not Interested", "Indication Interest", "Meeting Request", "RFQ",
        # Phase B parsing labels — both enqueue the add_to_crm action:
        "Add to CRM", "Out of Office"]

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
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


def env_opt(key: str) -> "str | None":
    if not ENV_PATH.exists():
        return None
    m = re.search(rf"{key}[^`]*`([^`]+)`", ENV_PATH.read_text(encoding="utf-8"))
    return m.group(1) if m else None


def hh(method: str, path: str, token: str, body=None):
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


def active_workspaces(slug: str | None):
    ref = env_value("SUPABASE_PROJECT_REF")
    key = env_opt("SUPABASE_SERVICE_ROLE_KEY") or env_value("SUPABASE_SECRET_KEY")
    params = {"select": "client_slug,hothawk_workspace_id", "is_active": "eq.true",
              "hothawk_workspace_id": "not.is.null", "order": "client_slug.asc"}
    if slug:
        params["client_slug"] = f"eq.{slug}"
    url = f"https://{ref}.supabase.co/rest/v1/workspaces?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"apikey": key, "Authorization": f"Bearer {key}",
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def label_ids(ws: str, token: str) -> dict[str, str]:
    s, b = hh("GET", f"/labels?workspaceId={ws}", token)
    if s != 200:
        return {}
    rows = b.get("data", b) if isinstance(b, dict) else b
    return {(x.get("name") or "").strip().lower(): x.get("id") for x in rows}


def specific_label_event_id(token: str, ws: str) -> str:
    s, payload = hh("GET", f"/events?workspaceId={ws}&category=SYSTEM", token)
    if s == 200 and isinstance(payload, list):
        for e in payload:
            if e.get("type") == "SPECIFIC_LABEL_ADDED" and e.get("id"):
                return e["id"]
    return SPECIFIC_LABEL_ADDED


def existing_label_webhooks(ws: str, token: str, dest: str) -> set[str]:
    """labelIds that already have a webhook pointing at our label_added endpoint."""
    s, hooks = hh("GET", f"/webhooks?workspaceId={ws}", token)
    hooks = hooks if isinstance(hooks, list) else []
    out = set()
    for h in hooks:
        if h.get("destinationUrl") == dest and h.get("labelId"):
            out.add(h["labelId"])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Register Go Capy label->action webhooks.")
    ap.add_argument("--apply", action="store_true", help="create missing webhooks (default: dry run)")
    ap.add_argument("--slug", help="limit to one client_slug")
    ap.add_argument("--list", action="store_true", help="just list which (workspace,label) webhooks exist")
    args = ap.parse_args()

    token = env_value("HOTHAWK_API_TOKEN")
    secret = env_value("HOTHAWK_WEBHOOK_SECRET")
    ref = env_value("SUPABASE_PROJECT_REF")
    dest = f"https://{ref}.supabase.co/functions/v1/email-ops-bridge/hothawk/{secret}/label_added"

    rows = active_workspaces(args.slug)
    print(f"{'APPLY' if args.apply else ('LIST' if args.list else 'DRY RUN')} — "
          f"{len(WANT)} label webhooks per workspace -> {dest}\n")
    created = existing = failed = missing_label = 0

    for r in rows:
        slug, ws = r["client_slug"], r["hothawk_workspace_id"]
        ids = label_ids(ws, token)
        have_lids = existing_label_webhooks(ws, token, dest)
        event_id = specific_label_event_id(token, ws)
        for name in WANT:
            lid = ids.get(name.lower())
            if not lid:
                missing_label += 1
                print(f"{slug:16} {name:20} NO LABEL — run ensure_labels.py first")
                continue
            if lid in have_lids:
                existing += 1
                if args.list:
                    print(f"{slug:16} {name:20} ✓ exists")
                continue
            if args.list:
                print(f"{slug:16} {name:20} — missing")
                continue
            if not args.apply:
                print(f"{slug:16} {name:20} would create (labelId={lid})")
                continue
            body = {"name": f"label-action: {name}", "destinationUrl": dest,
                    "workspaceId": ws, "eventId": event_id, "labelId": lid}
            code, resp = hh("POST", "/webhooks", token, body)
            if code in (200, 201):
                created += 1
                print(f"{slug:16} {name:20} created")
            else:
                failed += 1
                print(f"{slug:16} {name:20} FAIL({code}) {resp}")
            time.sleep(0.15)

    print(f"\ncreated={created} already-present={existing} missing-label={missing_label} "
          f"failed={failed} workspaces={len(rows)}")
    if not args.apply and not args.list:
        print("(dry run — re-run with --apply to create the webhooks shown above)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Ensure the Go Capy "Blacklist" + "Add to CRM" thread labels exist in every active HotHawk workspace.

The label->action automation (email-ops-bridge edge function) keys off HUMAN-applied thread labels.
The reply/CRM labels (Indication Interest, Meeting Request, RFQ, Wrong Person, Not Interested, Out of
Office, Automated Reply) already exist in every workspace as the old AI-categorisation labels, so this
script only CREATES the ones that are missing:

    Blacklist Contact          (per-principal contact blacklist)
    Blacklist Domain           (per-principal domain blacklist)
    Blacklis Contact All       (GLOBAL contact blacklist  -- NOTE: no "t": HotHawk caps names at 20 chars)
    Blacklist Domain All       (GLOBAL domain blacklist)
    Add to CRM                 (Phase B action; the label is created now, the handler ships later)

Idempotent: only creates a label that is absent (matched case-insensitively by name). Active workspaces
come from Supabase public.workspaces (source of truth) so this never drifts from a hardcoded list.

Usage:
    py ensure_labels.py            # DRY RUN: report present/missing, create nothing
    py ensure_labels.py --apply    # create any missing labels
    py ensure_labels.py --apply --slug tmx   # one workspace
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

# "Blacklis Contact All" intentionally drops the "t" (HotHawk caps label names at 20 chars).
WANT = ["Blacklist Contact", "Blacklist Domain", "Blacklis Contact All", "Blacklist Domain All", "Add to CRM"]

# Some HotHawk workspaces ship the reply labels under older names; rename them to the standard set so
# every workspace is identical before registering webhooks (interest/RFQ key off the exact names):
#   "Positive"          -> "Indication Interest"
#   "More Info Request" -> "RFQ"
# Do this via the HotHawk MCP labels_update (or the API) — renaming keeps the label id + tagged threads.
RENAME = {"positive": "Indication Interest", "more info request": "RFQ"}

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


def list_labels(ws: str, token: str):
    # GET /labels?workspaceId= works (the &take= variant 500s; omit it).
    s, b = hh("GET", f"/labels?workspaceId={ws}", token)
    if s != 200:
        return None
    rows = b.get("data", b) if isinstance(b, dict) else b
    return {(x.get("name") or "").strip().lower(): x.get("id") for x in rows}


def main() -> int:
    ap = argparse.ArgumentParser(description="Ensure Go Capy Blacklist/Add-to-CRM labels exist.")
    ap.add_argument("--apply", action="store_true", help="create missing labels (default: dry run)")
    ap.add_argument("--slug", help="limit to one client_slug")
    args = ap.parse_args()

    token = env_value("HOTHAWK_API_TOKEN")
    rows = active_workspaces(args.slug)
    print(f"{'APPLY' if args.apply else 'DRY RUN'} — ensuring {WANT} in {len(rows)} active workspaces\n")
    missing_total = created = failed = 0
    for r in rows:
        slug, ws = r["client_slug"], r["hothawk_workspace_id"]
        have = list_labels(ws, token)
        if have is None:
            print(f"{slug:16} ERROR listing labels — skipped")
            continue
        # Normalise legacy reply-label names to the standard set so all workspaces are identical.
        for old, new in RENAME.items():
            if old in have and new.lower() not in have:
                if not args.apply:
                    print(f"{slug:16} RENAME {old} -> {new}")
                    continue
                # HotHawk labels_update — confirm the route/method against the MCP if it 404s.
                code, _ = hh("PATCH", f"/labels/{have[old]}", token, {"name": new})
                print(f"{slug:16} rename {old} -> {new}: {'ok' if code in (200, 201) else f'FAIL({code})'}")
                if code in (200, 201):
                    have[new.lower()] = have.pop(old)
                time.sleep(0.15)
        states = []
        for name in WANT:
            if name.lower() in have:
                states.append(f"{name}=ok")
                continue
            missing_total += 1
            if not args.apply:
                states.append(f"{name}=MISSING")
                continue
            code, resp = hh("POST", "/labels", token, {"workspaceId": ws, "name": name})
            if code in (200, 201):
                created += 1
                states.append(f"{name}=created")
            else:
                failed += 1
                states.append(f"{name}=FAIL({code})")
            time.sleep(0.15)
        print(f"{slug:16} " + ", ".join(states))

    print(f"\nmissing={missing_total} created={created} failed={failed}"
          + ("" if args.apply else " — re-run with --apply to create them"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

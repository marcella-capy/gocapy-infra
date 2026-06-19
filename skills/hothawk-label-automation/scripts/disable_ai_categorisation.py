#!/usr/bin/env python3
"""Audit HotHawk AI auto-categorisation per active workspace (and guide turning it OFF).

WHY: the label->action automation requires labels to be HUMAN-applied. If HotHawk's AI auto-categorisation
is ON, it will apply reply labels (Indication Interest, RFQ, Wrong Person, ...) by itself, which would fire
our webhooks on the AI's guesses instead of a human decision.

WHAT THIS CAN / CANNOT DO:
- The workspace client-settings API (workspaces_settings_update) exposes only board/label-management/
  blocklist/LinkedIn/visibility flags — there is NO "AI categorisation" toggle there. The auto-categorise
  switch lives in the HotHawk inbox/AI settings (UI). So this script AUDITS rather than flips: per workspace
  it lists the labels that carry an AI rule (non-empty prompt / a sentiment / type AI_CATEGORISATION) — those
  are what auto-categorisation uses — so you can confirm the state and then turn it off in the UI.
- Optional --clear-prompts (apply): blanks each reply label's `prompt` via the labels update API, which
  removes the rule the auto-categoriser keys on. Use only if you also can't reach the UI toggle; confirm the
  labels update route/shape against the HotHawk MCP (labels_update) first.

MANUAL STEP (the reliable one): in each workspace's HotHawk inbox settings, turn OFF automatic AI
categorisation. Do this once per workspace at onboarding.

Usage:
    py disable_ai_categorisation.py            # audit all active workspaces (read-only)
    py disable_ai_categorisation.py --slug tmx # one workspace
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.hothawk.ai/v1"
ENV_PATH = Path(r"G:\Shared drives\Capy Outreach\global.env.md")

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


def hh(method: str, path: str, token: str):
    r = urllib.request.Request(f"{API}{path}", method=method, headers={"Authorization": f"Bearer {token}"})
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


def main() -> int:
    ap = argparse.ArgumentParser(description="Audit HotHawk AI auto-categorisation per workspace.")
    ap.add_argument("--slug", help="limit to one client_slug")
    args = ap.parse_args()

    token = env_value("HOTHAWK_API_TOKEN")
    rows = active_workspaces(args.slug)
    print("AI auto-categorisation audit — labels carrying an AI rule (prompt/sentiment/AI_CATEGORISATION).")
    print("Turn auto-categorisation OFF in each workspace's HotHawk inbox/AI settings (UI).\n")
    for r in rows:
        slug, ws = r["client_slug"], r["hothawk_workspace_id"]
        s, b = hh("GET", f"/labels?workspaceId={ws}", token)
        if s != 200:
            print(f"{slug:16} ERROR listing labels ({s})")
            continue
        labels = b.get("data", b) if isinstance(b, dict) else b
        ai = [x.get("name") for x in labels
              if (x.get("prompt") or x.get("sentiment") or x.get("type") == "AI_CATEGORISATION")]
        flag = "AI RULES PRESENT" if ai else "clean"
        print(f"{slug:16} {flag:18} {', '.join(n for n in ai if n)}")
    print("\nReminder: the client-settings API has no AI-categorise toggle — flip it in the UI per workspace.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

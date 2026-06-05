#!/usr/bin/env python3
"""PlusVibe warmup statistics — periodic health check for onboarded mailboxes.

Pulls per-account warmup health (and optionally the workspace inbox/spam/promotion
aggregate) so you can see how warmup is progressing for a principal over time.

- Per-account: warmup status + 7-day overall warmup health + warmup emails sent today,
  read from GET /account/list (each account's analytics.health_scores / daily_counters).
- Aggregate (optional, with --start-date/--end-date): GET /account/warmup-stats
  (inbox / spam / promotion placement of warmup mail over the range).

Auth & config: reads ~/.claude/global.env (team-shared) for PLUSVIBE_API_KEY and the
workspace-id vars (same file the plusvibe-api skill uses). Stdlib only.

Usage
-----
  py warmup_stats.py --workspace machining                  # all accounts in the ws
  py warmup_stats.py --workspace machining --tag 6a0cd81c4a80688441619120   # one principal (TMX)
  py warmup_stats.py --workspace machining --start-date 2026-05-28 --end-date 2026-06-04
  py warmup_stats.py --workspace forge --json
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_BASE = "https://api.plusvibe.ai/api/v1"
ENV_PATH = Path.home() / ".claude" / "global.env"

# Same aliases the plusvibe-api skill uses.
WORKSPACE_ALIASES = {
    "machining": "PLUSVIBE_MACHINING_WORKSPACE_ID",
    "forge": "PLUSVIBE_FORGE_WORKSPACE_ID",
}

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


def load_env() -> dict:
    if not ENV_PATH.exists():
        sys.exit(f"ERROR: {ENV_PATH} not found (team-shared env with PLUSVIBE_API_KEY).")
    env = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def resolve_workspace(alias_or_id: str, env: dict) -> str:
    if len(alias_or_id) == 24 and all(c in "0123456789abcdef" for c in alias_or_id.lower()):
        return alias_or_id
    key = WORKSPACE_ALIASES.get(alias_or_id.lower().strip())
    if not key:
        sys.exit(f"ERROR: unknown workspace '{alias_or_id}' (use machining | forge | <24-char id>)")
    ws = env.get(key, "")
    if not ws:
        sys.exit(f"ERROR: {key} missing from {ENV_PATH}; pass the raw workspace id instead.")
    return ws


def api_get(path: str, api_key: str, query: dict) -> object:
    q = {k: v for k, v in query.items() if v is not None}
    url = API_BASE + path + ("?" + urllib.parse.urlencode(q) if q else "")
    req = urllib.request.Request(url, headers={
        "x-api-key": api_key, "Accept": "application/json",
        "User-Agent": "gocapy-mailbox-onboarder/1.0",
    }, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        sys.exit(f"API ERROR {e.code} on GET {path}: {e.read().decode('utf-8', 'replace')[:300]}")
    except (urllib.error.URLError, TimeoutError) as e:
        sys.exit(f"API REQUEST FAILED on GET {path}: {e}")


def list_accounts(api_key: str, ws: str, tag: str | None) -> list[dict]:
    out, skip, limit = [], 0, 100
    while True:
        resp = api_get("/account/list", api_key,
                       {"workspace_id": ws, "skip": skip, "limit": limit, "tags": tag})
        batch = resp if isinstance(resp, list) else (resp.get("accounts") or resp.get("data") or [])
        out.extend(batch)
        if len(batch) < limit:
            break
        skip += limit
    return out


def warmup_fields(acc: dict) -> dict:
    """Pull warmup status/health/sent from an account record (defensive about nesting)."""
    payload = acc.get("payload") or acc
    analytics = payload.get("analytics") or acc.get("analytics") or {}
    health = (analytics.get("health_scores") or {})
    counters = (analytics.get("daily_counters") or {})
    return {
        "email": acc.get("email") or payload.get("email") or "",
        "warmup_status": acc.get("warmup_status") or payload.get("warmup_status") or "?",
        "health7d": health.get("7d_overall_warmup_health"),
        "google7d": health.get("7d_google_warmup_health"),
        "microsoft7d": health.get("7d_microsoft_warmup_health"),
        "warmup_sent_today": counters.get("warmup_email_sent_today"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="PlusVibe warmup statistics / health report.")
    ap.add_argument("--workspace", "-w", required=True, help="machining | forge | <24-char id>")
    ap.add_argument("--tag", help="filter to one principal/client tag id")
    ap.add_argument("--start-date", help="aggregate warmup-stats start (YYYY-MM-DD)")
    ap.add_argument("--end-date", help="aggregate warmup-stats end (YYYY-MM-DD)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    env = load_env()
    api_key = env.get("PLUSVIBE_API_KEY", "")
    if not api_key:
        sys.exit(f"ERROR: PLUSVIBE_API_KEY missing from {ENV_PATH}")
    ws = resolve_workspace(args.workspace, env)

    accounts = list_accounts(api_key, ws, args.tag)
    rows = [warmup_fields(a) for a in accounts]

    aggregate = None
    if args.start_date and args.end_date:
        aggregate = api_get("/account/warmup-stats", api_key,
                            {"workspace_id": ws, "start_date": args.start_date, "end_date": args.end_date})

    if args.json:
        print(json.dumps({"workspace": ws, "tag": args.tag, "count": len(rows),
                          "accounts": rows, "aggregate": aggregate}, indent=2))
        return 0

    print(f"Warmup health — workspace {ws}" + (f" · tag {args.tag}" if args.tag else "")
          + f" · {len(rows)} account(s)\n")
    healths = [r["health7d"] for r in rows if isinstance(r["health7d"], (int, float))]
    active = sum(1 for r in rows if str(r["warmup_status"]).upper() == "ACTIVE")
    low = [r for r in rows if isinstance(r["health7d"], (int, float)) and r["health7d"] < 90]
    if healths:
        print(f"  warmup ACTIVE: {active}/{len(rows)} · avg 7d health: {sum(healths)/len(healths):.0f}"
              f" · below 90%: {len(low)}\n")
    for r in sorted(rows, key=lambda x: (x["health7d"] is None, x["health7d"] or 0)):
        h = "n/a" if r["health7d"] is None else f"{r['health7d']}%"
        print(f"  {r['email']:<42} {str(r['warmup_status']):<9} health7d={h:<5} sent_today={r['warmup_sent_today']}")
    if low:
        print("\n  ⚠️ below 90% 7d warmup health:")
        for r in low:
            print(f"    {r['email']} — {r['health7d']}%")
    if aggregate is not None:
        print("\nAggregate warmup placement (range):")
        print(json.dumps(aggregate, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

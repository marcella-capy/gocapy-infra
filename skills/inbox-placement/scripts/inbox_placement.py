#!/usr/bin/env python3
"""PlusVibe inbox-placement (deliverability) tests — per principal.

A placement test sends seed emails from a principal's sending accounts to a panel of
inboxes and reports how many land in Inbox vs Spam vs Promotions. PlusVibe runs these as
AUTOMATIC "parent tests" that rotate across the workspace's sending accounts over time.

This wraps the PlusVibe email-placement API:
  list    GET  /email-placement/list/parent-tests
  create  POST /email-placement/create/parent-test     {name, type}
  result  GET  /email-placement/get/test-automatic-result  {test_id, sender_acc_id}

Auth/config: ~/.claude/global.env (PLUSVIBE_API_KEY + workspace ids). Stdlib only.

Usage
-----
  py inbox_placement.py list   -w machining
  py inbox_placement.py create -w machining --name "Tech-Max - Jun" --type AUTOMATIC
  py inbox_placement.py result -w machining --test-id <id> --sender-acc-id <acct id>
  py inbox_placement.py report -w machining            # inbox/spam summary per test

Note: AUTOMATIC tests auto-rotate sending accounts; how many accounts are sampled per run
is controlled by PlusVibe (verify in the UI if you need a specific rotation breadth).
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
        sys.exit(f"ERROR: unknown workspace '{alias_or_id}' (machining | forge | <24-char id>)")
    ws = env.get(key, "")
    if not ws:
        sys.exit(f"ERROR: {key} missing from {ENV_PATH}; pass the raw workspace id instead.")
    return ws


def api(method: str, path: str, api_key: str, query: dict | None = None, body: dict | None = None):
    q = {k: v for k, v in (query or {}).items() if v is not None}
    url = API_BASE + path + ("?" + urllib.parse.urlencode(q) if q else "")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"x-api-key": api_key, "Accept": "application/json",
               "User-Agent": "gocapy-inbox-placement/1.0"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        sys.exit(f"API ERROR {e.code} on {method} {path}: {e.read().decode('utf-8', 'replace')[:300]}")
    except (urllib.error.URLError, TimeoutError) as e:
        sys.exit(f"API REQUEST FAILED on {method} {path}: {e}")


def extract_tests(resp) -> list[dict]:
    if isinstance(resp, list):
        return resp
    data = resp.get("data") if isinstance(resp, dict) else None
    if isinstance(data, dict):
        return data.get("tests") or data.get("results") or data.get("items") or []
    if isinstance(data, list):
        return data
    return (resp or {}).get("tests") or []


def fmt_test(t: dict) -> str:
    tid = t.get("_id") or t.get("id") or ""
    name = t.get("name") or ""
    status = t.get("status") or ""
    sent = t.get("sent")
    inbox = t.get("inbox")
    spam = t.get("spam")
    promo = t.get("promotion")
    rate = ""
    if isinstance(sent, (int, float)) and sent:
        rate = f" · inbox {round(100 * (inbox or 0) / sent)}%"
    return (f"  {tid}  {status:<10} {name}\n"
            f"      sent={sent} inbox={inbox} spam={spam} promo={promo}{rate}")


def cmd_list(args, api_key, ws):
    resp = api("GET", "/email-placement/list/parent-tests", api_key,
               query={"workspace_id": ws, "status": args.status, "type": args.type,
                      "limit": args.limit, "page": args.page})
    if args.json:
        print(json.dumps(resp, indent=2)); return
    tests = extract_tests(resp)
    print(f"{len(tests)} parent test(s) in workspace {ws}:")
    for t in tests:
        print(fmt_test(t) if isinstance(t, dict) else f"  {t}")


def cmd_create(args, api_key, ws):
    resp = api("POST", "/email-placement/create/parent-test", api_key,
               body={"workspace_id": ws, "name": args.name, "type": args.type})
    print(json.dumps(resp, indent=2))


def cmd_duplicate(args, api_key, ws):
    """Clone a fully-configured 'golden' parent test into a new one.

    The duplicate INHERITS the source's UI config (send-as-plain-text, weekday
    schedule, Random selection mode, 5% sample, automation-off, start date) — the
    create API can't set any of those, so duplicating a configured template is the
    only way to replicate them. The copy lands as DRAFT and still points at the
    SOURCE's tag + campaign, so re-point those (UI-only) and start it. See SKILL.md.
    """
    resp = api("POST", "/email-placement/duplicate/parent-test", api_key,
               body={"workspace_id": ws, "parent_test_id": args.parent_test_id, "name": args.name})
    print(json.dumps(resp, indent=2))


def cmd_result(args, api_key, ws):
    resp = api("GET", "/email-placement/get/test-automatic-result", api_key,
               query={"workspace_id": ws, "test_id": args.test_id, "sender_acc_id": args.sender_acc_id})
    print(json.dumps(resp, indent=2))


def cmd_report(args, api_key, ws):
    resp = api("GET", "/email-placement/list/parent-tests", api_key,
               query={"workspace_id": ws})
    tests = extract_tests(resp)
    if args.name_contains:
        tests = [t for t in tests if args.name_contains.lower() in (t.get("name", "").lower())]
    if not tests:
        print("(no matching placement tests)"); return
    print(f"Inbox-placement summary — workspace {ws} · {len(tests)} test(s)\n")
    for t in sorted(tests, key=lambda x: x.get("modified_at", ""), reverse=True):
        print(fmt_test(t))


def main() -> int:
    p = argparse.ArgumentParser(description="PlusVibe inbox-placement (deliverability) tests")
    p.add_argument("--workspace", "-w", required=True, help="machining | forge | <24-char id>")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list"); pl.add_argument("--status"); pl.add_argument("--type")
    pl.add_argument("--limit", type=int); pl.add_argument("--page", type=int)
    pl.add_argument("--json", action="store_true"); pl.set_defaults(func=cmd_list)

    pc = sub.add_parser("create"); pc.add_argument("--name", required=True)
    pc.add_argument("--type", choices=["AUTOMATIC", "MANUAL"], default="AUTOMATIC")
    pc.set_defaults(func=cmd_create)

    pdup = sub.add_parser("duplicate", help="clone a configured golden test (inherits UI config)")
    pdup.add_argument("--parent-test-id", required=True, help="source (golden) test id to clone")
    pdup.add_argument("--name", required=True, help="new test name, e.g. 'Alpha Grainger - Jun'")
    pdup.set_defaults(func=cmd_duplicate)

    pr = sub.add_parser("result"); pr.add_argument("--test-id", required=True)
    pr.add_argument("--sender-acc-id", required=True); pr.set_defaults(func=cmd_result)

    prep = sub.add_parser("report"); prep.add_argument("--name-contains")
    prep.set_defaults(func=cmd_report)

    args = p.parse_args()
    env = load_env()
    api_key = env.get("PLUSVIBE_API_KEY", "")
    if not api_key:
        sys.exit(f"ERROR: PLUSVIBE_API_KEY missing from {ENV_PATH}")
    ws = resolve_workspace(args.workspace, env)
    return args.func(args, api_key, ws) or 0


if __name__ == "__main__":
    raise SystemExit(main())

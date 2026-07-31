#!/usr/bin/env python3
"""Daily HotHawk mailbox health scan across every workspace.

Why this exists
---------------
HotHawk shows a freshly-added mailbox as "Gathering data" (status GATHERING) even when
its IMAP/SMTP credentials are wrong. A bad-cred mailbox silently retries auth against the
underlying SiteGround server; SiteGround doesn't whitelist our domains, so the retries get
the HotHawk sending IP blocklisted. This scan catches the fallout daily: disconnected
accounts (reconnect them, or remove the dead ones), and mailboxes stuck "gathering" far
longer than a healthy one ever should.

What it does
------------
1. Pulls the live workspace list (GET /v1/workspaces) — never the stale hardcoded slug
   tables in block_all.py / backfill_hothawk_blocklist.py, which disagree with each other.
2. For each workspace, pages GET /v1/mailboxes and buckets by currentConnectionStatus
   (mirrors the UI's Total / Connected / Disconnected / Gathering tiles).
3. Tracks per-account state across runs in scripts/state/health_state.json so it can tell a
   mailbox that JUST started gathering from one that's been stuck for hours.
4. Remediation (only with --remediate; report-only by default):
     - DISCONNECTED + creds on file        -> reconnect (delete + re-add via connect-imap),
                                              recorded so a still-dead account next run is
                                              deleted + flagged instead of looping forever.
     - DISCONNECTED + no creds              -> flag for manual handling (never blind-delete).
     - stuck GATHERING/INITIALIZING > N h   -> flag only (never auto-delete a gatherer).
5. Posts one Discord digest (via notify_discord.py) — silent unless something is actionable,
   or always with --always-notify.

Reconnect note: HotHawk exposes no REST reconnect route (only the MCP reconnect_mailbox tool,
unavailable to a headless cron). connect-imap + delete ARE REST-reachable, so "reconnect" here
is delete-then-re-add with the stored creds — functionally identical for a dead mailbox.

Credentials (gitignored): G:\\Shared drives\\Capy Outreach\\hothawk-mailbox-creds.json
    {
      "domains":   {"lnp-machining.com": {"host":"mail.lnp-machining.com",
                                          "smtpPort":465,"imapPort":993,"password":"...",
                                          "usernameStyle":"email"}},
      "overrides": {"someone@domain.com": {"host":"...","smtpPort":465,"imapPort":993,
                                          "password":"...","username":"someone@domain.com"}}
    }
Without it the scan still reports; it just can't auto-reconnect.

Usage
-----
    py check_mailbox_health.py                 # report + digest, no changes (safe default)
    py check_mailbox_health.py --remediate     # also reconnect / delete dead accounts
    py check_mailbox_health.py --workspace "LNP Machining"   # one workspace only
    py check_mailbox_health.py --no-discord    # never post (local inspection)
    py check_mailbox_health.py --always-notify # post even when everything's healthy

Stdlib only. Exit 0 on a clean run (even if it flagged things); non-zero on a hard error.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.hothawk.ai/v1"
ENV_PATH = Path(r"G:\Shared drives\Capy Outreach\global.env.md")
CREDS_PATH = Path(r"G:\Shared drives\Capy Outreach\hothawk-mailbox-creds.json")

SKILL_DIR = Path(__file__).resolve().parent.parent           # .../skills/hothawk-mailbox-connect
STATE_PATH = SKILL_DIR / "scripts" / "state" / "health_state.json"
NOTIFY_DISCORD = (
    SKILL_DIR.parent / "ai-sdr-manager" / "scripts" / "notify_discord.py"
)

# A healthy mailbox on these domains reaches CONNECTED within ~30 min (LNP calibration run:
# 16 healthy mailboxes were still GATHERING at ~20 min with 0 disconnected). Past this, a
# gatherer is suspect — flag it (we never auto-delete a gatherer; the add flow owns that).
STUCK_GATHERING_HOURS = 6

CONNECTED = "CONNECTED"
DISCONNECTED = "DISCONNECTED"
GATHERING_STATES = {"GATHERING", "INITIALIZING"}
# A disconnected account we already tried to reconnect this many hours ago, still dead,
# is treated as truly dead -> delete + flag (don't churn it every single run).
RECONNECT_GRACE_HOURS = 20

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def hours_since(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return 0.0
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds() / 3600.0


# --------------------------------------------------------------------------- env / creds

def get_token() -> str:
    if not ENV_PATH.exists():
        sys.exit(f"error: global env not found at {ENV_PATH}")
    txt = ENV_PATH.read_text(encoding="utf-8")
    m = re.search(r"HOTHAWK_API_TOKEN[^`]*`([^`]+)`", txt)
    if not m:
        sys.exit("error: HOTHAWK_API_TOKEN not found in env file")
    return m.group(1)


def load_creds() -> dict:
    """Optional credentials reference. Returns {} if absent (report-only still works).

    NOTE (2026-07-31): CREDS_PATH does not currently exist on the Shared Drive, so --remediate is
    a silent no-op. Per-mailbox passwords now live in the per-principal CSVs under
    `G:\\Shared drives\\Capy Outreach\\Cold Email Accounts\\<Principal>\\` (mandated by the
    siteground skill, "Output 3"). Rebuild this file from those, or point CREDS_PATH at them,
    before trusting --remediate. The caller warns loudly rather than pretending to remediate.
    """
    if not CREDS_PATH.exists():
        print(f"warn: creds file not found at {CREDS_PATH} — reconnect/remediate unavailable",
              file=sys.stderr)
        return {}
    try:
        return json.loads(CREDS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"warn: could not read creds file ({e}); reconnect disabled", file=sys.stderr)
        return {}


def creds_for(email: str, domain: str, creds: dict) -> dict | None:
    """Resolve connect-imap params for an address: per-email override beats per-domain."""
    entry = (creds.get("overrides") or {}).get(email) or (creds.get("domains") or {}).get(domain)
    if not entry or not entry.get("password") or not entry.get("host"):
        return None
    username = entry.get("username") or email  # usernameStyle defaults to the full address
    return {
        "imapHost": entry["host"], "imapPort": int(entry.get("imapPort", 993)),
        "smtpHost": entry["host"], "smtpPort": int(entry.get("smtpPort", 465)),
        "imapUsername": username, "smtpUsername": username,
        "imapPassword": entry["password"], "smtpPassword": entry["password"],
    }


# --------------------------------------------------------------------------- HTTP

def _request(method: str, path: str, token: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {token}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{API}{path}", data=data, method=method, headers=headers)
    for attempt, backoff in enumerate((2, 8, 32)):  # retry 429/5xx
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


def delete_mailbox(token: str, mb_id: str) -> bool:
    status, _ = _request("DELETE", f"/mailboxes/{mb_id}", token)
    return status in (200, 204)


def connect_imap(token: str, ws_id: str, mb: dict, cfg: dict) -> tuple[int, dict | None]:
    first, _, last = (mb.get("fullName") or "").partition(" ")
    body = {
        "firstName": first or "Mailbox", "lastName": last or "User",
        "email": mb["email"], "workspaceId": ws_id, **cfg,
    }
    return _request("POST", "/mailboxes/connect-imap", token, body)


def reconnect(token: str, ws_id: str, mb: dict, cfg: dict) -> bool:
    """Delete then re-add with stored creds (no REST reconnect route exists)."""
    if not delete_mailbox(token, mb["id"]):
        return False
    status, _ = connect_imap(token, ws_id, mb, cfg)
    return status in (200, 201)


# --------------------------------------------------------------------------- state

def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- digest

def post_discord(message: str) -> None:
    if not NOTIFY_DISCORD.exists():
        print("warn: notify_discord.py not found; printing digest instead", file=sys.stderr)
        print(message)
        return
    try:
        subprocess.run(["py", str(NOTIFY_DISCORD), "-"], input=message,
                       text=True, encoding="utf-8", check=False, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"warn: discord post failed ({e}); digest below\n{message}", file=sys.stderr)


# --------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="Daily HotHawk mailbox health scan.")
    ap.add_argument("--remediate", action="store_true",
                    help="reconnect / delete dead accounts (default: report only)")
    ap.add_argument("--workspace", help="limit to one workspace by name (case-insensitive)")
    ap.add_argument("--no-discord", action="store_true", help="never post to Discord")
    ap.add_argument("--always-notify", action="store_true",
                    help="post the digest even when everything is healthy")
    ap.add_argument("--stuck-hours", type=float, default=STUCK_GATHERING_HOURS,
                    help=f"flag gatherers older than this (default {STUCK_GATHERING_HOURS})")
    args = ap.parse_args()

    token = get_token()
    creds = load_creds()
    if args.remediate and not creds:
        # Reconnect is delete + re-add, so without passwords --remediate can only DELETE. Refuse
        # rather than quietly destroying mailboxes we cannot put back.
        sys.exit("error: --remediate requires credentials, but none were loaded "
                 f"({CREDS_PATH} is missing). Re-run report-only, or restore the creds file.")
    state = load_state()
    seen_ids: set[str] = set()

    workspaces = list_workspaces(token)
    if args.workspace:
        workspaces = [w for w in workspaces if w["name"].lower() == args.workspace.lower()]
        if not workspaces:
            sys.exit(f"error: no workspace named '{args.workspace}'")

    lines: list[str] = []          # per-workspace summary lines
    reconnected: list[str] = []
    deleted: list[str] = []
    needs_manual: list[str] = []
    stuck: list[str] = []
    totals = {"total": 0, CONNECTED: 0, DISCONNECTED: 0, "GATHERING": 0, "other": 0}

    for ws in workspaces:
        boxes = list_mailboxes(token, ws["id"])
        buckets = {CONNECTED: 0, DISCONNECTED: 0, "GATHERING": 0, "other": 0}
        for mb in boxes:
            st = mb.get("currentConnectionStatus", "other")
            key = CONNECTED if st == CONNECTED else \
                  DISCONNECTED if st == DISCONNECTED else \
                  "GATHERING" if st in GATHERING_STATES else "other"
            buckets[key] += 1
            totals[key] += 1
            totals["total"] += 1

            mb_id = mb["id"]
            seen_ids.add(mb_id)
            rec = state.setdefault(mb_id, {"firstSeen": now_iso()})
            rec["email"] = mb.get("email")
            rec["workspace"] = ws["name"]
            rec["status"] = st
            rec["lastSeen"] = now_iso()
            if st in GATHERING_STATES:
                rec.setdefault("gatheringSince", now_iso())
            else:
                rec.pop("gatheringSince", None)

            label = f"{mb.get('email')} ({ws['name']})"

            if st == DISCONNECTED:
                cfg = creds_for(mb.get("email", ""), mb.get("domain", ""), creds)
                if not args.remediate:
                    needs_manual.append(f"{label} — disconnected")
                elif cfg is None:
                    needs_manual.append(f"{label} — disconnected, no creds on file")
                elif hours_since(rec.get("reconnectAttemptedAt")) and \
                        hours_since(rec.get("reconnectAttemptedAt")) < RECONNECT_GRACE_HOURS:
                    needs_manual.append(f"{label} — reconnect already attempted, still down")
                elif rec.get("reconnectAttemptedAt"):
                    # tried before, grace elapsed, still dead -> remove it
                    if delete_mailbox(token, mb_id):
                        deleted.append(f"{label} — dead after reconnect, removed")
                        state.pop(mb_id, None)
                        seen_ids.discard(mb_id)
                    else:
                        needs_manual.append(f"{label} — delete failed")
                else:
                    if reconnect(token, ws["id"], mb, cfg):
                        rec["reconnectAttemptedAt"] = now_iso()
                        reconnected.append(f"{label} — reconnect attempted")
                    else:
                        needs_manual.append(f"{label} — reconnect call failed")

            elif st in GATHERING_STATES:
                if hours_since(rec.get("gatheringSince")) >= args.stuck_hours:
                    h = round(hours_since(rec.get("gatheringSince")))
                    stuck.append(f"{label} — stuck gathering ~{h}h")

        lines.append(
            f"**{ws['name']}** — {len(boxes)} total · "
            f"{buckets[CONNECTED]} connected · {buckets[DISCONNECTED]} disconnected · "
            f"{buckets['GATHERING']} gathering"
            + (f" · {buckets['other']} other" if buckets["other"] else "")
        )

    # prune state for mailboxes that no longer exist
    for gone in [k for k in state if k not in seen_ids]:
        state.pop(gone, None)
    save_state(state)

    actionable = bool(reconnected or deleted or needs_manual or stuck)

    header = "🩺 HotHawk mailbox health — " + datetime.now(timezone.utc).strftime("%Y-%m-%d")
    mode = "" if args.remediate else "  (report-only)"
    digest = [f"{header}{mode}",
              f"{totals['total']} mailboxes · {totals[CONNECTED]} connected · "
              f"{totals[DISCONNECTED]} disconnected · {totals['GATHERING']} gathering", ""]
    digest += lines
    if reconnected:
        digest += ["", "🔄 Reconnect attempted:"] + [f"• {x}" for x in reconnected]
    if deleted:
        digest += ["", "🗑️ Removed (dead):"] + [f"• {x}" for x in deleted]
    if stuck:
        digest += ["", "⏳ Stuck gathering (check creds / SiteGround):"] + [f"• {x}" for x in stuck]
    if needs_manual:
        digest += ["", "🚩 Needs you — delete or fix auth:"] + [f"• {x}" for x in needs_manual]
    if not actionable:
        digest += ["", "✅ All accounts healthy."]
    text = "\n".join(digest)

    print(text)
    if not args.no_discord and (actionable or args.always_notify):
        post_discord(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

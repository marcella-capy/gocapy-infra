#!/usr/bin/env python3
"""Call -> HotHawk load audit.

Answers one question, on a schedule: of the call-task-scheduler calls a rep marked DONE in a given
window, did every person actually get ENROLLED (and sending) in their principal's HotHawk voicemail
campaign? A done call is supposed to trigger a HotHawk subscribe (see the call-task-scheduler skill);
this audit independently re-checks the outcome against LIVE HotHawk enrollment, so a silently-dead
reconcile (or a draft campaign that holds leads on the list but never sends) gets caught and pinged.

It reads two independent live sources — it does NOT trust _reconcile_state.json:
  * DONE set  = Pipedrive done call activities matching this skill's subject contract, in the window
                (reuses call-task-scheduler/create_call_tasks.principal_of + reconcile's window logic).
  * LOADED set = leads present in the principal's HotHawk campaign via GET /campaigns/{seq}/leads
                (a lead only appears there once ENROLLED; a draft campaign returns none -> flagged).

Report columns (per Marcella): principal | User (Ericka/Mark/Jon/...) | Company Name | # of Calls.
Any person whose done call did NOT reach HotHawk is listed in a separate NOT-LOADED section.

Modes (window):
  day    previous business day                       (daily weekday run)
  week   Monday-of-this-week .. previous business day (Friday-morning run: the week so far)
  month  the whole PREVIOUS calendar month           (1st-of-month run)

CLI:
    python audit_call_loads.py --mode day|week|month [--date YYYY-MM-DD] [--discord]
    (without --discord it prints the report to stdout; with --discord it also posts to the AISDR
     Discord webhook. --date overrides "today" for backfills/testing.)
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).resolve().parent
# The call-task-scheduler scripts carry the Pipedrive helpers, registry, USERS map, capy_env and
# business_days; the outreach plugin carries the pd_cache person/org snapshot.
CTS_SCRIPTS = HERE.parent.parent / "call-task-scheduler" / "scripts"
OUTREACH_SCRIPTS = (Path.home() / ".claude" / "plugins" / "marketplaces"
                    / "gocapy-claude-plugin" / "go-capy-outreach" / "scripts")
sys.path.insert(0, str(CTS_SCRIPTS))
sys.path.insert(0, str(OUTREACH_SCRIPTS))

import capy_env  # noqa: E402
import pd_cache  # noqa: E402
from business_days import previous_business_day  # noqa: E402
from create_call_tasks import (REGISTRY, USERS, _pd, _primary_email,  # noqa: E402
                               activity_person_id, principal_of)

HH_BASE = "https://api.hothawk.ai/v1"
# Display overrides so the User column reads the way the desk refers to reps.
USER_DISPLAY = {"Jonathan": "Jon"}
MONTHS = ["", "January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]


# --------------------------------------------------------------------------- windows
def window_for(mode: str, today: _dt.date) -> "tuple[_dt.date, _dt.date, str]":
    """(start, end, label) for the audit window ending relative to `today`."""
    if mode == "day":
        d = previous_business_day(today)
        return d, d, f"Daily — {d.isoformat()}"
    if mode == "week":
        end = previous_business_day(today)
        start = end - _dt.timedelta(days=end.weekday())  # Monday of end's week
        return start, end, f"Weekly — {start.isoformat()} .. {end.isoformat()}"
    if mode == "month":
        first_this = today.replace(day=1)
        last_prev = first_this - _dt.timedelta(days=1)
        start = last_prev.replace(day=1)
        return start, last_prev, f"Monthly — {MONTHS[start.month]} {start.year}"
    raise SystemExit(f"unknown mode: {mode}")


# --------------------------------------------------------------------------- pipedrive
def done_marker_activities(start: _dt.date, end: _dt.date, registry: dict) -> list:
    """This skill's call activities marked done within [start, end], as (activity, slug) pairs."""
    out, cursor = [], None
    since = f"{start.isoformat()}T00:00:00Z"
    while True:
        r = _pd("GET", "/activities",
                {"done": "true", "updated_since": since, "limit": 500, "cursor": cursor})
        for act in r.get("data") or []:
            slug = principal_of(act, registry)
            if not slug:
                continue
            done_ts = act.get("marked_as_done_time") or act.get("update_time") or ""
            if start.isoformat() <= str(done_ts)[:10] <= end.isoformat():
                out.append((act, slug))
        cursor = (r.get("additional_data") or {}).get("next_cursor")
        if not cursor:
            break
    return out


def person_lookup(pid: int, persons_idx: dict) -> "dict | None":
    """Snapshot first; fall back to a live Pipedrive fetch for people newer than the daily cache."""
    p = persons_idx.get(str(pid))
    if p:
        return p
    try:
        return (_pd("GET", f"/persons/{pid}").get("data") or None)
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError):
        return None


# --------------------------------------------------------------------------- hothawk
def _hh_get(path: str, token: str) -> dict:
    req = urllib.request.Request(HH_BASE + path, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def enrolled_emails(seq_id: str, token: str, cache: dict) -> "dict[str, str]":
    """{lower(email): status} for every lead ENROLLED in the campaign (paginated). Empty for a
    draft campaign (no enrolled leads) -> its done calls will read as NOT loaded, which is the point."""
    if seq_id in cache:
        return cache[seq_id]
    emails: dict[str, str] = {}
    page = 1
    while True:
        try:
            d = _hh_get(f"/campaigns/{seq_id}/leads?limit=25&page={page}", token)
        except (urllib.error.HTTPError, urllib.error.URLError, ValueError):
            break  # campaign missing/deleted -> treat as no enrollment
        rows = d.get("data") or []
        for row in rows:
            e = (row.get("leadEmail") or "").strip().lower()
            if e:
                emails[e] = row.get("status") or ""
        pages = (d.get("meta") or {}).get("pagesCount") or 1
        if page >= pages or not rows:
            break
        page += 1
    cache[seq_id] = emails
    return emails


def lead_enrolled(ws_id: str, email: str, token: str) -> bool:
    """True if the lead has a non-null campaignStatus (ENROLLED) in its workspace — catches leads
    freshly enrolled at `not_contacted` that the send-queue endpoint (enrolled_emails) hasn't picked
    up yet. `campaignStatus == None` = on-list-only / draft = a real miss. Used only to re-verify the
    handful the bulk send-queue check flags as missing, so the per-lead call cost stays small.
    The search endpoint is authoritative here (it updates ahead of the stale GET /crm/leads/{id})."""
    try:
        d = _hh_get("/crm/leads?" + urllib.parse.urlencode(
            {"workspaceId": ws_id, "search": email, "take": 5}), token)
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError):
        return False
    for row in (d.get("data") or []):
        if (row.get("email") or "").strip().lower() == email.strip().lower():
            return row.get("campaignStatus") is not None
    return False


# --------------------------------------------------------------------------- report
def _fixed_table(headers: list, rows: list) -> str:
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(str(c)))
    line = lambda cells: "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(cells)).rstrip()
    sep = "  ".join("-" * w for w in widths)
    return "\n".join([line(headers), sep] + [line(r) for r in rows])


def build_report(mode: str, today: _dt.date) -> "tuple[str, dict]":
    start, end, label = window_for(mode, today)
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    token = capy_env.get("HOTHAWK_API_TOKEN")
    if not token:
        raise SystemExit("ERROR: HOTHAWK_API_TOKEN missing in env")

    persons_idx = {str(p.get("id")): p for p in pd_cache.get_persons()}
    orgs = pd_cache.get_orgs()

    done = done_marker_activities(start, end, registry)

    # per (principal, user, company) call counts for the roster table
    calls: dict = {}
    # per person: dedup for the load check (a person can have several done calls)
    people: dict = {}
    hh_cache: dict = {}

    for act, principal in done:
        owner_id = act.get("owner_id")
        user = USER_DISPLAY.get(USERS.get(owner_id, ""), USERS.get(owner_id, str(owner_id)))
        pid = activity_person_id(act)
        p = person_lookup(pid, persons_idx) if pid else None
        if p:
            rel = p.get("org_id")
            oid = rel.get("value") if isinstance(rel, dict) else rel
            company = (orgs.get(str(oid), {}) or {}).get("name") or p.get("org_name") or "(unknown)"
            email = _primary_email(p) or ""
            name = p.get("name") or f"person {pid}"
        else:
            company, email, name = "(unknown)", "", (f"person {pid}" if pid else "(no participant)")

        calls[(principal, user, company)] = calls.get((principal, user, company), 0) + 1

        if pid and pid not in people:
            reg = registry.get(principal) or {}
            seq = reg.get("sequence_id")
            if "@" not in email:                       # missing, or a placeholder like "invalid"
                loaded, reason = False, "no-email"
            elif not seq:
                loaded, reason = False, "no-sequence-in-registry"
            else:
                status = enrolled_emails(seq, token, hh_cache).get(email.lower())
                loaded = status is not None
                # Send-queue miss? Re-verify at the lead level — a lead freshly enrolled at
                # `not_contacted` is loaded but not yet in the send queue. Only the flagged few pay this.
                if not loaded and reg.get("workspace_id") and lead_enrolled(reg["workspace_id"], email, token):
                    loaded = True
                reason = "" if loaded else "not-enrolled (on list only / draft campaign?)"
            people[pid] = {"principal": principal, "user": user, "company": company,
                           "name": name, "email": email, "loaded": loaded, "reason": reason}

    loaded_ct = sum(1 for v in people.values() if v["loaded"])
    missing = [v for v in people.values() if not v["loaded"]]

    # -- roster table (all calls) + a per-User totals footer
    roster_rows = sorted(([pr, us, co, n] for (pr, us, co), n in calls.items()),
                         key=lambda r: (r[0], r[1], r[2]))
    per_user: dict = {}
    for (pr, us, co), n in calls.items():
        per_user[us] = per_user.get(us, 0) + n
    footer = [["", f"TOTAL {us}", "", cnt] for us, cnt in sorted(per_user.items())]
    if roster_rows:
        roster = _fixed_table(["principal", "User", "Company Name", "# of Calls"],
                              roster_rows + [["-", "-", "-", "-"]] + footer)
    else:
        roster = "(no done calls in window)"

    total_calls = len(done)
    head = "✅ all loaded" if not missing else f"⚠️ {len(missing)} NOT loaded"
    lines = [f"\U0001f4de Call → HotHawk Load Audit — {label}",
             f"{head}  ·  {total_calls} calls / {len(people)} people  ·  "
             f"{loaded_ct} loaded, {len(missing)} missing",
             "```", roster, "```"]

    if missing:
        mrows = sorted(([m["principal"], m["user"], m["company"],
                         (m["name"] + (f" <{m['email']}>" if m["email"] else "")), m["reason"]]
                        for m in missing), key=lambda r: (r[0], r[1], r[2]))
        lines += [f"⚠️ NOT LOADED ({len(missing)}) — done calls that never reached HotHawk:",
                  "```", _fixed_table(["principal", "User", "Company", "Person", "why"], mrows), "```",
                  "Fix: activate/enroll the campaign in HotHawk, or re-run the reconcile "
                  "(`reconcile_done_calls.py --since <date> --apply`) for these people."]

    roster_only = "\n".join([f"\U0001f4de Call Roster — {label}", "```", roster, "```"])
    meta = {"mode": mode, "window": [start.isoformat(), end.isoformat()],
            "total_calls": total_calls, "people": len(people),
            "loaded": loaded_ct, "missing": len(missing)}
    return "\n".join(lines), meta, roster_only


# --------------------------------------------------------------------------- discord
def post_discord(text: str) -> None:
    """Chunk on line boundaries under Discord's limit and post each via kodie_notify.post_discord."""
    url = capy_env.get("DISCORD_AISDR_WEBHOOK_URL") or capy_env.get("DISCORD_INBOUND_WEBHOOK_URL")
    if not url:
        raise SystemExit("--discord needs DISCORD_AISDR_WEBHOOK_URL (or DISCORD_INBOUND_WEBHOOK_URL) in env")
    clickup_scripts = (Path.home() / ".claude" / "plugins" / "marketplaces"
                       / "gocapy-claude-plugin" / "go-capy-outreach" / "skills" / "clickup" / "scripts")
    sys.path.insert(0, str(clickup_scripts))
    from kodie_notify import post_discord as _pd_discord  # noqa: E402

    chunk, size, in_code = [], 0, False
    for line in text.splitlines():
        if size + len(line) + 1 > 1800 and chunk:
            if in_code:                      # keep code fences balanced across a split
                chunk.append("```")
            _pd_discord(url, "\n".join(chunk))
            chunk, size = (["```"] if in_code else []), (4 if in_code else 0)
        if line.strip() == "```":
            in_code = not in_code
        chunk.append(line)
        size += len(line) + 1
    if chunk:
        _pd_discord(url, "\n".join(chunk))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=("day", "week", "month"), default="day")
    ap.add_argument("--date", help="override 'today' (YYYY-MM-DD) for backfills/testing")
    ap.add_argument("--discord", action="store_true", help="post the report to the AISDR Discord")
    ap.add_argument("--roster-only", action="store_true",
                    help="report/post ONLY the roster table (no load-check headline or NOT-LOADED section)")
    a = ap.parse_args()

    today = _dt.date.fromisoformat(a.date) if a.date else _dt.date.today()
    report, meta, roster_only = build_report(a.mode, today)
    out = roster_only if a.roster_only else report
    print(out)
    if a.discord:
        post_discord(out)
        print("\ndiscord: posted")
    print(f"\nRESULT: ok - {json.dumps(meta)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

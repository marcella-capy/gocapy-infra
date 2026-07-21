#!/usr/bin/env python3
"""Pre-call verification sweep (v2.2, rep feedback 2026-07-10) — items 2/3/4:
verify LinkedIn title+company, pull missing LinkedIn via Clay, sanity-check phones.

Scope: OPEN Call-1 activities from this skill due within --days business days (default 3).
Only the people who made the 25-cap selection are verified — never the whole org — so
Bright Data spend is ~CALLS_PER_DAY x days per company per sweep.

Per person (skipped if the Call-1 note already carries a `[verified YYYY-MM-DD]` marker):
  1. Has LinkedIn -> Bright Data enrich scrape (batched). If the profile's current
     company doesn't match the task's org: flag the note and (with --apply, unless
     --no-close) auto-close ALL of the person's open call tasks with an audit note —
     marked done, never deleted — so the next sheet re-run rotates a replacement in.
     If title differs from Pipedrive: flag `LinkedIn title: <title>` (informational).
     Private/hidden profiles are flagged unverifiable, tasks kept.
  2. No LinkedIn -> push to the Clay People table (LinkedIn-URL data point writes back
     to Pipedrive). Same guardrails as task creation: tier-1 title, CLAY_EXCLUDE
     blocklist, max 10 per run.
  3. Phones -> phone_format_ok (free, local). All-invalid -> flag + close (same rule as
     departed). Valid numbers -> pushed to the standalone Clay phone-validation webhook
     (`phone_validation` key in references/clay-webhooks.json) when configured; silently
     skipped with a report line when the table doesn't exist yet.

CLI:
    python verify_call_contacts.py [--days 3] [--max-brightdata 25] [--apply] [--no-close]

Default is a dry-run report. Ends with `RESULT: ok|error - <details>`.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
PLUGIN = Path.home() / ".claude" / "plugins" / "marketplaces" / "gocapy-claude-plugin"
sys.path.insert(0, str(PLUGIN / "go-capy-outreach" / "scripts"))
sys.path.insert(0, str(PLUGIN / "go-capy-outreach" / "skills" / "person-research-agent-v3" / "scripts"))
from business_days import add_business_days  # noqa: E402
from create_call_tasks import (  # noqa: E402
    CLAY_WEBHOOKS, LINKEDIN_KEY, TITLE_KEY, _clay_post, _pd, _phones, _primary_email,
    _split_name, activity_person_id, clay_eligible, load_registry, phone_format_ok,
    principal_of, tier_of, CLAY_PEOPLE_MAX,
)

VERIFIED_RE = re.compile(r"\[verified \d{4}-\d{2}-\d{2}\]")


def _open_call1s(registry: dict, horizon: "_dt.date") -> list:
    """Open Call-1 activities from this skill due on or before `horizon`."""
    out, cursor = [], None
    while True:
        r = _pd("GET", "/activities", {"done": "false", "limit": 500, "cursor": cursor})
        for act in r.get("data") or []:
            if not principal_of(act, registry):
                continue
            subject = act.get("subject") or ""
            if not (re.search(r": Call 1 - ", subject) or subject.startswith("Call 1:")):
                continue
            due = act.get("due_date") or ""
            if due and due <= horizon.isoformat():
                out.append(act)
        cursor = (r.get("additional_data") or {}).get("next_cursor")
        if not cursor:
            break
    return out


def _open_calls_for(person_id: int, registry: dict) -> list:
    ids, cursor = [], None
    while True:
        r = _pd("GET", "/activities", {"done": "false", "limit": 500, "cursor": cursor})
        for act in r.get("data") or []:
            if principal_of(act, registry) and activity_person_id(act) == person_id:
                ids.append(act)
        cursor = (r.get("additional_data") or {}).get("next_cursor")
        if not cursor:
            break
    return ids


def _company_matches(profile: dict, org_name: str) -> "bool | None":
    """True/False = confident verdict from the scraped current company; None = can't tell
    (private profile, empty fields) — callers must NOT close tasks on None."""
    if profile.get("error"):
        return None
    cur = profile.get("current_company")
    cur_name = (cur.get("name") if isinstance(cur, dict) else str(cur or "")) or ""
    hay = (cur_name + " " + str(profile.get("position") or "")
           + " " + str(profile.get("current_company_name") or "")).lower()
    if not hay.strip():
        return None
    toks = [t for t in re.split(r"[^a-z0-9]+", (org_name or "").lower())
            if len(t) > 2 and t not in ("inc", "llc", "corp", "the", "and", "company", "technologies")]
    if not toks:
        return None
    return any(t in hay for t in toks)


def _flag_note(act: dict, lines: list, apply: bool, stamp: bool) -> None:
    add = "<br>".join(lines)
    if stamp:
        add += f"<br>[verified {_dt.date.today().isoformat()}]"
    if apply:
        _pd("PATCH", f"/activities/{act['id']}", body={"note": add + "<br><br>" + (act.get("note") or "")})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=3, help="business-day lookahead window")
    ap.add_argument("--max-brightdata", type=int, default=25, help="spend cap per sweep")
    ap.add_argument("--apply", action="store_true", help="write flags/closes (default: dry-run)")
    ap.add_argument("--no-close", action="store_true",
                    help="never auto-close tasks, only flag notes")
    a = ap.parse_args()

    registry = load_registry()
    horizon = add_business_days(_dt.date.today(), a.days)
    call1s = _open_call1s(registry, horizon)
    print(f"open Call-1 tasks due <= {horizon}: {len(call1s)}")

    # one entry per person; skip already-verified
    by_person: dict = {}
    for act in call1s:
        pid = activity_person_id(act)
        if not pid or pid in by_person:
            continue
        if VERIFIED_RE.search(act.get("note") or ""):
            continue
        by_person[pid] = act
    print(f"to verify (unverified people): {len(by_person)}")

    clay = json.loads(CLAY_WEBHOOKS.read_text(encoding="utf-8"))
    phone_webhook = clay.get("phone_validation")

    # live person fetch (small volume; Clay may have written phones/LinkedIn after snapshot)
    people = {}
    for pid in by_person:
        people[pid] = (_pd("GET", f"/persons/{pid}", version="v1").get("data") or {})

    # ── Bright Data batch for everyone WITH a LinkedIn URL ─────────────────────────────
    with_li = {pid: p for pid, p in people.items() if (p.get(LINKEDIN_KEY) or "").strip()}
    without_li = {pid: p for pid, p in people.items() if pid not in with_li}
    li_urls = {pid: p[LINKEDIN_KEY].strip() for pid, p in with_li.items()}
    capped = dict(list(li_urls.items())[:a.max_brightdata])
    if len(li_urls) > len(capped):
        print(f"  Bright Data cap: verifying {len(capped)} of {len(li_urls)} LinkedIn profiles this sweep")

    profiles_by_pid: dict = {}
    if capped and a.apply:
        from brightdata_linkedin import fetch_profiles
        profiles = fetch_profiles(list(capped.values()))
        by_url = {}
        for prof in profiles:
            u = (prof.get("url") or prof.get("input_url")
                 or (prof.get("input") or {}).get("url") or "")
            by_url[u.rstrip("/").lower()] = prof
        for pid, u in capped.items():
            profiles_by_pid[pid] = by_url.get(u.rstrip("/").lower())
    elif capped:
        print(f"  DRY-RUN: would Bright-Data-scrape {len(capped)} profiles")

    flags = closes = clay_li = clay_phone = 0
    registry_full = registry
    for pid, act in by_person.items():
        p = people[pid]
        org_rel = p.get("org_id")
        org_name = (org_rel.get("name") if isinstance(org_rel, dict) else "") or ""
        notes, close, stamp = [], False, True

        # 1. LinkedIn verify / pull
        if pid in with_li:
            prof = profiles_by_pid.get(pid)
            if prof is None and a.apply and pid in capped:
                notes.append("⚠ LinkedIn: profile could not be scraped (private/unavailable)")
            elif prof is not None:
                match = _company_matches(prof, org_name)
                li_title = str(prof.get("position") or "").strip()
                if match is False:
                    cur = prof.get("current_company")
                    cur_name = (cur.get("name") if isinstance(cur, dict) else str(cur or "")) or "?"
                    notes.append(f"⚠ LinkedIn: no longer at {org_name} — now {li_title or '?'} @ {cur_name}")
                    close = True
                elif match is None:
                    notes.append("⚠ LinkedIn: could not confirm current employer")
                elif li_title and li_title.lower() != (p.get(TITLE_KEY) or "").strip().lower():
                    notes.append(f"LinkedIn title: {li_title}")
            elif pid not in capped:
                stamp = False  # over the spend cap — leave unstamped so next sweep picks it up
        else:
            # no LinkedIn -> Clay pull (same guardrails as creation)
            if tier_of(p) == 1 and clay_eligible(p) and clay_li < CLAY_PEOPLE_MAX:
                first, last = _split_name(p)
                oid = org_rel.get("value") if isinstance(org_rel, dict) else org_rel
                payload = {"name": p.get("name"), "first_name": first, "last_name": last,
                           "job_title": p.get(TITLE_KEY) or "", "email": _primary_email(p),
                           "linkedin_url": "", "company_name": org_name,
                           "company_domain": ((_primary_email(p).split("@", 1) + [""])[1]),
                           "pipedrive_person_id": int(pid),
                           "pipedrive_org_id": int(oid) if oid else None}
                if a.apply:
                    if _clay_post(clay["people_no_phone"], payload):
                        clay_li += 1
                else:
                    print(f"  DRY-RUN: would push {p.get('name')} to Clay People table (LinkedIn pull)")
                    clay_li += 1
            notes.append("no LinkedIn on file" + (" — sent to Clay" if tier_of(p) == 1 and clay_eligible(p) else ""))

        # 2. phone format check + Clay phone-validation push
        phones = _phones(p)
        valid = [v for v in phones if phone_format_ok(v)]
        bad = [v for v in phones if not phone_format_ok(v)]
        for v in bad:
            notes.append(f"⚠ phone invalid: {v}")
        if phones and not valid:
            close = True
        if valid and phone_webhook:
            payload = {"name": p.get("name"), "phones": valid,
                       "pipedrive_person_id": int(pid), "company_name": org_name}
            if a.apply:
                if _clay_post(phone_webhook, payload):
                    clay_phone += 1
            else:
                clay_phone += 1
        elif valid and not phone_webhook:
            pass  # phone-validation table not built yet — free check only

        if a.no_close:
            close = False

        if notes:
            flags += 1
            print(f"  {p.get('name')} (person {pid}): " + " | ".join(notes)
                  + (" -> CLOSE open tasks" if close else ""))
        if close:
            audit = f"auto-closed by pre-call verification {_dt.date.today().isoformat()}: " \
                    + "; ".join(n.replace("⚠ ", "") for n in notes)
            for sib in _open_calls_for(pid, registry_full):
                if a.apply:
                    note = (sib.get("note") or "") + "<br>" + audit
                    _pd("PATCH", f"/activities/{sib['id']}", body={"done": True, "note": note})
                else:
                    print(f"    DRY-RUN would close activity {sib['id']}: {sib.get('subject')}")
                closes += 1
        elif notes or (pid in capped or pid in without_li):
            _flag_note(act, notes or ["verified ok"], a.apply, stamp)

    mode = "" if a.apply else "DRY-RUN "
    if not phone_webhook:
        print("  NOTE: clay-webhooks.json has no 'phone_validation' key — live phone check skipped "
              "(free format check still ran)")
    print(f"RESULT: ok - {mode}{len(by_person)} people checked, {flags} flagged, "
          f"{closes} tasks closed, clay: {clay_li} LinkedIn pulls / {clay_phone} phone validations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Retrofit open call tasks to the current rules (template; first used 2026-07-08).

For every OPEN activity matching the skill's subject contract:
  1. --rename (2026-07-10): rewrite legacy subjects "Call <n>: <Last> from <Org> for <Display>"
     to the principal-first format "<Display>: Call <n> - <Last> from <Org>".
  2. Territory: for principals with a territory rule (Patriot Forge, Tech-Max, General Foundry,
     Harvey Vogel, Megatech), DELETE all open tasks of out-of-territory people.
  3. Pacing: per org, sort remaining people by title tier (tier-1 mgmt sourcing titles first)
     and re-date their open Call 1/2/3 to the CALLS_PER_DAY batch scheme starting next business
     day (Call n due = today + [1,5,7][n-1] + batch business days, batch = index // CALLS_PER_DAY).
     People whose Call 1 is no longer open (started calling) are left untouched.

Default is a DRY-RUN report; pass --apply to write. --rename-only skips territory/pacing.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from collections import defaultdict
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
OUTREACH_SCRIPTS = (Path.home() / ".claude" / "plugins" / "marketplaces"
                    / "gocapy-claude-plugin" / "go-capy-outreach" / "scripts")
sys.path.insert(0, str(OUTREACH_SCRIPTS))
import territory_filter  # noqa: E402
from business_days import add_business_days  # noqa: E402
from create_call_tasks import (  # noqa: E402
    _pd, activity_person_id, principal_of, tier_of, CALLS_PER_DAY, CALL_OFFSETS,
    MAX_PEOPLE_PER_RUN, PERSON_STATE_KEY, PERSON_CITY_KEY, PERSON_COUNTRY_KEY, ORG_STATE_KEY,
    TERRITORY_SLUGS, TITLE_KEY, REGISTRY, SUBJECT_RE, OLD_SUBJECT_RE,
)


def _open_skill_activities(registry):
    out, cursor = [], None
    while True:
        r = _pd("GET", "/activities", {"done": "false", "limit": 500, "cursor": cursor})
        for a in r.get("data") or []:
            slug = principal_of(a, registry)
            if slug:
                a["_slug"] = slug
                out.append(a)
        cursor = (r.get("additional_data") or {}).get("next_cursor")
        if not cursor:
            break
    return out


import re

# legacy subject with the tail split out so it can be re-assembled principal-first;
# greedy (.+) makes the LAST " for " the separator, so org names containing " for " survive
_LEGACY_PARTS_RE = re.compile(r"^Call ([123]): (.+) for (.+)$")


def _call_n(subject: str) -> "int | None":
    m = SUBJECT_RE.match(subject)
    if m:
        return int(m.group(2))
    m = OLD_SUBJECT_RE.match(subject)
    return int(m.group(1)) if m else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run report)")
    ap.add_argument("--rename", action="store_true",
                    help="rewrite legacy subjects to the principal-first format")
    ap.add_argument("--rename-only", action="store_true",
                    help="only rename; skip territory deletes and re-pacing")
    a = ap.parse_args()

    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    acts = _open_skill_activities(registry)
    print(f"open skill activities: {len(acts)}")

    total_renamed = 0
    if a.rename or a.rename_only:
        for act in acts:
            m = _LEGACY_PARTS_RE.match(act.get("subject") or "")
            if not m:
                continue  # already principal-first (or not ours — filtered earlier)
            new_subject = f"{m.group(3)}: Call {m.group(1)} - {m.group(2)}"
            print(f"  rename {act['id']}: {act['subject']!r} -> {new_subject!r}")
            if a.apply:
                _pd("PATCH", f"/activities/{act['id']}", body={"subject": new_subject})
            act["subject"] = new_subject
            total_renamed += 1
        print(f"renamed: {total_renamed}{'' if a.apply else ' (DRY-RUN)'}")
        if a.rename_only:
            print(f"RESULT: ok - {'APPLIED' if a.apply else 'DRY-RUN'}: {total_renamed} subjects renamed")
            return 0

    # group: org -> person -> {call_n: activity}
    by_org = defaultdict(lambda: defaultdict(dict))
    org_slug = {}
    for act in acts:
        oid = act.get("org_id")
        pid = activity_person_id(act)
        n = _call_n(act.get("subject") or "")
        if not oid or not pid or not n:
            continue
        by_org[int(oid)][int(pid)][n] = act
        org_slug[int(oid)] = act["_slug"]

    persons_cache, orgs_cache = {}, {}

    def person(pid):
        if pid not in persons_cache:
            persons_cache[pid] = (_pd("GET", f"/persons/{pid}", version="v1").get("data") or {})
        return persons_cache[pid]

    def org_state(oid):
        if oid not in orgs_cache:
            orgs_cache[oid] = (_pd("GET", f"/organizations/{oid}", version="v1").get("data") or {})
        return str(orgs_cache[oid].get(ORG_STATE_KEY) or "")

    today = _dt.date.today()
    total_del = total_redated = 0

    for oid, people in sorted(by_org.items()):
        slug = org_slug[oid]
        terr_slug = TERRITORY_SLUGS.get(slug, slug)
        org_name = ""
        keep_people = []
        deleted_people = []

        for pid, calls in people.items():
            p = person(pid)
            org_name = org_name or ((p.get("org_id") or {}).get("name") if isinstance(p.get("org_id"), dict) else "") or ""
            ok, reason = territory_filter.keep(terr_slug, {
                "state": p.get(PERSON_STATE_KEY) or org_state(oid),
                "city": p.get(PERSON_CITY_KEY) or "",
                "country": p.get(PERSON_COUNTRY_KEY) or "",
            })
            if ok:
                keep_people.append((pid, p, calls))
            else:
                deleted_people.append((pid, p, calls, reason))

        print(f"\norg {oid} ({org_name or '?'}) [{slug}]: {len(people)} people with open tasks")

        for pid, p, calls, reason in deleted_people:
            ids = [c["id"] for c in calls.values()]
            print(f"  DELETE {p.get('name')} ({reason}) -> activities {ids}")
            if a.apply:
                for aid in ids:
                    _pd("DELETE", f"/activities/{aid}")
            total_del += len(ids)

        # re-date: only people whose Call 1 is still open (untouched sequences), tier order
        fresh = [(pid, p, calls) for pid, p, calls in keep_people if 1 in calls]
        started = len(keep_people) - len(fresh)
        fresh.sort(key=lambda t: (tier_of(t[1]), t[0]))

        # apply the new 25-person cap: started people count toward it; tasks of everyone
        # beyond the cap are deleted (they can come back via a re-run after 45 days)
        cap_room = max(0, MAX_PEOPLE_PER_RUN - started)
        overflow = fresh[cap_room:]
        fresh = fresh[:cap_room]
        for pid, p, calls in overflow:
            ids = [c["id"] for c in calls.values()]
            print(f"  DELETE {p.get('name')} (tier {tier_of(p)}, over cap {MAX_PEOPLE_PER_RUN}) "
                  f"-> activities {ids}")
            if a.apply:
                for aid in ids:
                    _pd("DELETE", f"/activities/{aid}")
            total_del += len(ids)

        for idx, (pid, p, calls) in enumerate(fresh):
            batch = idx // CALLS_PER_DAY
            for n, act in sorted(calls.items()):
                new_due = add_business_days(today, CALL_OFFSETS[n - 1] + batch).isoformat()
                if act.get("due_date") == new_due:
                    continue
                print(f"  redate {p.get('name')} (tier {tier_of(p)}) Call {n}: "
                      f"{act.get('due_date')} -> {new_due}")
                if a.apply:
                    _pd("PATCH", f"/activities/{act['id']}", body={"due_date": new_due})
                total_redated += 1
        if started:
            print(f"  {started} people already started (Call 1 done) — untouched")

    mode = "APPLIED" if a.apply else "DRY-RUN"
    print(f"\nRESULT: ok - {mode}: {total_renamed} subjects renamed, "
          f"{total_del} activities deleted (out-of-territory), "
          f"{total_redated} due dates changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

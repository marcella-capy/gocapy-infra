#!/usr/bin/env python3
"""Workflow B of call-task-scheduler: find call activities from this skill that were marked DONE on
the target day; the FIRST done call per person triggers a HotHawk voicemail-sequence subscribe.

This script does the deterministic part only:
  1. pulls done activities (v2, updated_since, cursor pagination), filters to this skill's note
     marker, keeps those whose marked-done time falls on --date;
  2. for each person not already in the state file, builds the full HotHawk lead payload from the
     snapshot (first name, last name, email, company name, phone) + the principal's registry entry;
     persons with no email are flagged skipped:no-email, NOT subscribed;
  3. writes _reconcile_<date>.json with a `to_subscribe` list. The AGENT then performs the HotHawk
     MCP calls per SKILL.md (crm_leads_list -> crm_leads_create -> subsequences_subscribe_create)
     and re-runs this script with --mark-subscribed <person_id,...> --apply to record them in
     _reconcile_state.json (idempotency across days).

Sibling call tasks stay OPEN by default (Marcella 2026-07-15). Pass --close-siblings to also
mark the person's remaining open call activities done (PATCH done=true; never deletes unless
--delete-remaining). The old --no-close-siblings flag is accepted as a no-op — it is now the
default. Do not flip this back without her say-so: the 2026-07-30 catch-up auto-closed 59 tasks
because the default was still the old one.

CLI:
    python reconcile_done_calls.py [--date YYYY-MM-DD] [--apply]
    python reconcile_done_calls.py --mark-subscribed 123,456 --apply [--close-siblings]
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
OUTREACH_SCRIPTS = Path.home() / ".claude" / "plugins" / "marketplaces" / "gocapy-claude-plugin" / "go-capy-outreach" / "scripts"
sys.path.insert(0, str(OUTREACH_SCRIPTS))  # pd_cache snapshot lives in the outreach plugin
import pd_cache  # noqa: E402
from business_days import previous_business_day  # noqa: E402
from create_call_tasks import (REGISTRY, TITLE_KEY, _pd, _phones,  # noqa: E402
                               _primary_email, activity_person_id, load_registry, principal_of)

STATE = HERE / "_reconcile_state.json"


def _load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _done_marker_activities(date: _dt.date, registry: dict, end: "_dt.date | None" = None) -> list:
    """This skill's call activities marked done on `date` (or in [date, end] when `end` is given),
    as (activity, principal_slug) pairs (client-side filtered on the subject contract)."""
    out, cursor = [], None
    end = end or date
    since = f"{date.isoformat()}T00:00:00Z"
    while True:
        r = _pd("GET", "/activities",
                {"done": "true", "updated_since": since, "limit": 500, "cursor": cursor})
        for act in r.get("data") or []:
            slug = principal_of(act, registry)
            if not slug:
                continue
            done_ts = act.get("marked_as_done_time") or act.get("update_time") or ""
            if date.isoformat() <= str(done_ts)[:10] <= end.isoformat():
                out.append((act, slug))
        cursor = (r.get("additional_data") or {}).get("next_cursor")
        if not cursor:
            break
    return out


def _split_name(p: dict) -> "tuple[str, str]":
    first = (p.get("first_name") or "").strip()
    last = (p.get("last_name") or "").strip()
    if first or last:
        return first, last
    parts = (p.get("name") or "").strip().split()
    return (" ".join(parts[:-1]), parts[-1]) if len(parts) > 1 else (parts[0] if parts else "", "")


def _open_sibling_ids(person_id: int) -> list:
    """Open call activities from this skill for one person (to auto-close after subscribe)."""
    ids, cursor = [], None
    registry = load_registry()
    while True:
        r = _pd("GET", "/activities", {"done": "false", "limit": 500, "cursor": cursor})
        for act in r.get("data") or []:
            if principal_of(act, registry) and activity_person_id(act) == person_id:
                ids.append(act["id"])
        cursor = (r.get("additional_data") or {}).get("next_cursor")
        if not cursor:
            break
    return ids


def _sequence_for(pid: int) -> "str | None":
    """Sequence id the person was queued under, from the newest reconcile artifact that lists them."""
    for artifact in sorted(HERE.glob("_reconcile_*.json"), reverse=True):
        try:
            for e in json.loads(artifact.read_text(encoding="utf-8")).get("to_subscribe", []):
                if e.get("person_id") == pid:
                    return e.get("sequence_id")
        except (json.JSONDecodeError, OSError):
            continue
    return None


def _mark_subscribed(person_ids: list, delete_remaining: bool, apply: bool,
                     close_siblings: bool = False) -> int:
    state = _load_state()
    closed = 0
    now = _dt.datetime.now().isoformat(timespec="seconds")
    for pid in person_ids:
        sibs = _open_sibling_ids(pid) if close_siblings else []
        for aid in sibs:
            if not apply:
                print(f"  DRY-RUN would auto-close activity {aid} for person {pid}")
                continue
            if delete_remaining:
                _pd("DELETE", f"/activities/{aid}")
            else:
                act = (_pd("GET", f"/activities/{aid}").get("data") or {})
                note = (act.get("note") or "") + f"<br>auto-closed: subscribed to HotHawk voicemail sequence {now[:10]}"
                _pd("PATCH", f"/activities/{aid}", body={"done": True, "note": note})
            closed += 1
        if apply:
            state[str(pid)] = {"subscribed_at": now, "sequence_id": _sequence_for(pid),
                               "closed_activities": sibs}
    if apply:
        STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(f"RESULT: ok - marked {len(person_ids)} subscribed, {closed} sibling activities "
          f"{'deleted' if delete_remaining else 'auto-closed'}{'' if apply else ' (DRY-RUN)'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", help="target day (YYYY-MM-DD); default: previous business day")
    ap.add_argument("--since", help="start of a date RANGE (YYYY-MM-DD, inclusive through --date/"
                                    "today) — use for catch-ups so skipped days can't fall through")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--mark-subscribed", help="comma-separated person_ids the agent just subscribed")
    ap.add_argument("--close-siblings", action="store_true",
                    help="ALSO mark the person's remaining open call tasks done. OFF by default "
                         "(Marcella 2026-07-15: sibling call tasks stay open).")
    ap.add_argument("--delete-remaining", action="store_true",
                    help="with --close-siblings, delete the sibling call tasks instead of marking "
                         "them done (default: mark done)")
    ap.add_argument("--no-close-siblings", action="store_true", help=argparse.SUPPRESS)  # no-op
    a = ap.parse_args()

    if a.mark_subscribed:
        pids = [int(s) for s in a.mark_subscribed.split(",") if s.strip()]
        return _mark_subscribed(pids, a.delete_remaining, a.apply,
                                close_siblings=a.close_siblings)

    date = _dt.date.fromisoformat(a.date) if a.date else previous_business_day(_dt.date.today())
    if a.since:
        start, end = _dt.date.fromisoformat(a.since), (date if a.date else _dt.date.today())
    else:
        start, end = date, date
    state = _load_state()
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    persons_idx = {str(p.get("id")): p for p in pd_cache.get_persons()}
    orgs = pd_cache.get_orgs()

    done = _done_marker_activities(start, registry, end=end)
    to_subscribe, skipped = [], []
    seen: set = set()
    for act, principal in done:
        pid = activity_person_id(act)
        if not pid:
            skipped.append({"activity_id": act.get("id"), "reason": "no-participant"})
            continue
        if pid in seen or str(pid) in state:
            continue  # already subscribed (state) or already queued this run
        seen.add(pid)
        p = persons_idx.get(str(pid))
        if not p:
            skipped.append({"person_id": pid, "reason": "not-in-snapshot"})
            continue
        reg = registry.get(principal)
        if not reg:
            skipped.append({"person_id": pid, "reason": f"principal '{principal}' not in registry"})
            continue
        email = _primary_email(p)
        if not email:
            skipped.append({"person_id": pid, "name": p.get("name"), "reason": "no-email"})
            continue
        first, last = _split_name(p)
        rel = p.get("org_id")
        oid = rel.get("value") if isinstance(rel, dict) else rel
        company = (orgs.get(str(oid), {}) or {}).get("name") or p.get("org_name") or ""
        to_subscribe.append({
            "person_id": pid, "principal": principal,
            "first_name": first, "last_name": last, "email": email,
            "company_name": company, "phone": (_phones(p) or [""])[0],
            "title": p.get(TITLE_KEY) or "",
            "workspace_id": reg["workspace_id"], "sequence_id": reg["sequence_id"],
            "done_activity_id": act.get("id"),
        })

    artifact = HERE / (f"_reconcile_{start.strftime('%Y%m%d')}.json" if start == end else
                       f"_reconcile_{start.strftime('%Y%m%d')}-{end.strftime('%Y%m%d')}.json")
    payload = {"date": start.isoformat(), "date_end": end.isoformat(), "done_calls": len(done),
               "to_subscribe": to_subscribe, "skipped": skipped}
    artifact.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    for e in to_subscribe:
        print(f"  to-subscribe: {e['first_name']} {e['last_name']} <{e['email']}> ({e['company_name']}) "
              f"-> seq {e['sequence_id']} [{e['principal']}]")
    for s in skipped:
        print(f"  skipped: {s}")
    span = start.isoformat() if start == end else f"{start}..{end}"
    print(f"RESULT: ok - {len(done)} done calls on {span}, {len(to_subscribe)} to-subscribe, "
          f"{len(skipped)} skipped -> {artifact.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

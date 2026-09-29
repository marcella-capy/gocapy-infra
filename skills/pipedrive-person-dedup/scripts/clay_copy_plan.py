#!/usr/bin/env python3
"""Plan CSV for the Clay "Find People -> Update Pipedrive" copies (2026-09-28 incident).

WHY: Clay table t_0tkuubw8rbpp7mnDYmQ re-ran its "Create person" action on all 2,150 rows every
day from 09-22 to 09-27 with no lookup, so those people now hold ~3,500 extra Pipedrive records.
The general monthly plan (dedup_persons.py) picks the survivor by engagement then lowest id,
which keeps a NEW empty copy in ~460 cases. Marcella's rule for these copies (2026-09-28):
keep the OLDEST record that has an email (else the oldest), merge every same-company copy into
it, hold people who appear at two companies for review, keep one record for the 924 people whose
first record was itself a Clay create.

This is a DRY RUN generator: zero Pipedrive writes. Its output feeds the existing executor:
    python dedup_persons.py --execute --plan <clay_copies_plan_YYYYMMDD.csv> --tiers high --limit 10

Usage:
    python clay_copy_plan.py [--rows FILE] [--capy-root DIR] [--post-task 86bc8kj30]
      --rows FILE   a saved Clay row dump (from clay_cli.table_rows or the CLI's raw rows);
                    without it the table is read live through the Clay CLI (no credits).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dedup_persons as dp  # noqa: E402  (also puts dedup_common on the path)
dc = dp.dc
log = dc.log

TABLE = "t_0tkuubw8rbpp7mnDYmQ"
LINKEDIN_COL = "LinkedIn Profile"
CLAY_ACTOR = "20845253"          # Clay's Pipedrive user; our scripts are 22638704
MATCH_ON = "clay-t1-copy"


def _cell(row: dict, name: str):
    """Value of a column from either row shape (flat {name: value} or {cells: {name: {value}}})."""
    if "cells" in row:
        c = (row["cells"] or {}).get(name) or {}
        return c.get("value") if c.get("status") == "success" else None
    return row.get(name)


def load_rows(path: "str | None", root: Path) -> list:
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    sys.path.insert(0, str(root / "gocapy-claude-plugin/go-capy-outreach/skills/clay/scripts"))
    import clay_cli
    return clay_cli.table_rows(TABLE)


def owner_of(p) -> str:
    o = p.get("owner_id")
    return str(o.get("id") if isinstance(o, dict) else o or "")


def build(persons: list, slugs: set, li_key: str, merged: set) -> tuple[list, list, dict]:
    groups = defaultdict(list)
    for p in persons:
        if p.get("active_flag") is False:
            continue
        s = dc.linkedin_slug(p.get(li_key), "in")
        if s and s in slugs:
            groups[s].append(p)

    plan, review = [], []
    st = Counter()
    for s, ps in groups.items():
        if len(ps) < 2:
            st["people_with_one_record"] += 1
            continue
        ps.sort(key=lambda p: (not dp.emails_of(p), p.get("add_time") or "", int(p["id"])))
        surv = ps[0]
        st["people_with_copies"] += 1
        if owner_of(surv) == CLAY_ACTOR:
            st["kept_record_is_clay_made"] += 1
        sname = dp.Name(surv.get("name") or "")
        orgs = {dp.org_of(p) for p in ps}
        multi_org = len(orgs) > 1
        if multi_org:
            st["people_at_two_companies"] += 1
        for p in ps[1:]:
            if int(p["id"]) in merged:
                st["already_merged"] += 1
                continue
            agree = dp.names_agree(sname, dp.Name(p.get("name") or ""))
            row = {
                "tier": "high", "match_on": MATCH_ON,
                "name_match": {2: "exact", 1: "fuzzy", 0: "differs"}[agree],
                "survivor_id": surv["id"], "survivor_name": surv.get("name"),
                "survivor_org": surv.get("org_name"), "survivor_email": ", ".join(dp.emails_of(surv)),
                "survivor_reason": "oldest with email" if dp.emails_of(surv) else "oldest (no record has an email)",
                "survivor_added": (surv.get("add_time") or "")[:10],
                "loser_id": p["id"], "loser_name": p.get("name"), "loser_org": p.get("org_name"),
                "loser_email": ", ".join(dp.emails_of(p)), "loser_added": (p.get("add_time") or "")[:10],
                "note": "",
            }
            if dp.emails_of(p):
                st["copies_with_email"] += 1
            if dp.engagement(p):
                st["copies_with_activity"] += 1
            if multi_org and dp.org_of(p) != dp.org_of(surv):
                row["tier"], row["note"] = "review", "at a different company than the kept record - job change?"
                review.append(row)
            elif agree == 0:
                row["tier"], row["note"] = "review", "name differs from the kept record"
                review.append(row)
            else:
                plan.append(row)
    st["merges_planned"] = len(plan)
    st["held_for_review"] = len(review)
    plan.sort(key=lambda r: (int(r["survivor_id"]), int(r["loser_id"])))
    review.sort(key=lambda r: (int(r["survivor_id"]), int(r["loser_id"])))
    return plan, review, dict(st)


def report_text(st: dict, plan_name: str, review_name: str) -> str:
    return (
        "Clean-up list for the Clay copies is ready - nothing has been merged.\n\n"
        f"{st.get('people_with_copies', 0):,} people from the Clay Find People table have more than one "
        f"Pipedrive record. I would merge {st.get('merges_planned', 0):,} copies into the record we keep. "
        "The kept record is the oldest one with an email (or simply the oldest when none has one); "
        f"for {st.get('kept_record_is_clay_made', 0):,} people that oldest record is itself the one Clay "
        "added, because they were new to Pipedrive.\n\n"
        f"- {st.get('copies_with_email', 0):,} copies picked up an email later: it moves onto the kept record.\n"
        f"- {st.get('copies_with_activity', 0):,} copies carry notes or activity: those move too, nothing is lost.\n"
        f"- {st.get('held_for_review', 0):,} are held for you ({review_name}): "
        f"{st.get('people_at_two_companies', 0):,} people appear at two different companies "
        "(possible job change, never merged automatically) and the rest have a name that does not "
        "match.\n\n"
        "Names and phone numbers are never changed by a merge. "
        f"The full list is attached ({plan_name}). Reply \"merge the Clay copies\" and I start with 10 "
        "people so we can check the result together, then run the rest over two or three nights "
        "inside Pipedrive's daily limit."
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rows", default=None)
    ap.add_argument("--capy-root", default=None)
    ap.add_argument("--post-task", default=None)
    a = ap.parse_args()
    root = dc.find_capy_root(a.capy_root, dp.SKILL)
    if root is None:
        log("ERROR: could not locate gocapy-claude-plugin. Pass --capy-root.")
        return 2
    dc.bootstrap(root)
    import pd_cache
    import pd_fields
    dp.NICK.update(dp.load_nicknames())

    rows = load_rows(a.rows, root)
    slugs = {dc.linkedin_slug(_cell(r, LINKEDIN_COL), "in") for r in rows} - {""}
    log(f"[clay-copies] {len(rows)} Clay rows, {len(slugs)} LinkedIn slugs")

    base = root / dc.DEDUP_OUT_REL / "persons"
    merged = set()
    ledger = base / "merged_ledger.jsonl"
    if ledger.exists():
        for line in ledger.read_text(encoding="utf-8").splitlines():
            try:
                merged.add(int(json.loads(line).get("loser")))
            except (ValueError, TypeError, json.JSONDecodeError):
                continue

    plan, review, st = build(pd_cache.get_persons(), slugs, pd_fields.PERSON_LINKEDIN, merged)
    out = dc.out_dir(root, "persons")
    day = dc.today()
    plan_path = out / f"clay_copies_plan_{day}.csv"
    review_path = out / f"clay_copies_review_{day}.csv"
    dc.write_csv(plan_path, dp.CSV_HEADER, plan)
    dc.write_csv(review_path, dp.CSV_HEADER, review)
    text = report_text(st, plan_path.name, review_path.name)
    (out / f"clay_copies_report_{day}.txt").write_text(text, encoding="utf-8")
    (out / f"clay_copies_summary_{day}.json").write_text(
        json.dumps({"date": day, "snapshot": pd_cache.snapshot_date("persons"), "table": TABLE, **st},
                   indent=2), encoding="utf-8")
    log(json.dumps(st, indent=2))
    log(f"[clay-copies] DRY RUN - zero Pipedrive writes. Plan: {plan_path}")
    if a.post_task:
        ok = dc.post_to_clickup(root, a.post_task, text, [plan_path, review_path])
        log(f"[clay-copies] ClickUp post {'ok' if ok else 'FAILED'} on task {a.post_task}")
    print(f"RESULT: clay-copies dry-run merges={st.get('merges_planned', 0)} "
          f"review={st.get('held_for_review', 0)} csv={plan_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

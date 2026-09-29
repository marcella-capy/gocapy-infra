#!/usr/bin/env python3
"""Delete the EMPTY Clay copies; leave everything else to the merge executor.

WHY (Marcella 2026-09-29): "if the record is empty, doesn't it make more sense to just delete
the duplicate record? all this is very recent". A delete is reversible for 30 days (Pipedrive
keeps deleted people that long), a merge is not, and it costs a fraction of the tokens.
This is a NARROW EXCEPTION to the skill's merge-only rule: it applies ONLY to copies that
  * are listed as a loser in a clay_copies_plan CSV (built by clay_copy_plan.py),
  * were created by Clay's Pipedrive user (20845253) on or after 2026-09-04,
  * hold no email, no phone, no notes / activities / deals / files / mail, and
  * hold no field value that the kept record is missing.
Anything else goes to the merge list and is handled by dedup_persons.py --execute.

Every delete re-reads BOTH records live right before acting, saves the full copy to the audit
log first, and is recorded in deleted_ledger.jsonl. Names and phones are never written.

    python clay_copy_cleanup.py split   --plan clay_copies_plan_<date>.csv       # snapshot only
    python clay_copy_cleanup.py delete  --plan clay_copies_delete_<date>.csv [--limit N] [--execute]
    python clay_copy_cleanup.py restore --id <person id> [--execute]            # undo one delete
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dedup_persons as dp  # noqa: E402
dc = dp.dc
log = dc.log

CLAY_ACTOR = "20845253"
EARLIEST = "2026-09-04"
COUNTS = ("email_messages_count", "activities_count", "done_activities_count", "undone_activities_count",
          "notes_count", "files_count", "open_deals_count", "closed_deals_count",
          "related_open_deals_count", "related_closed_deals_count",
          "participant_open_deals_count", "participant_closed_deals_count")
NOT_DATA = set(COUNTS) | {
    "id", "owner_id", "org_id", "name", "first_name", "last_name", "first_char", "add_time", "update_time",
    "delete_time", "active_flag", "visible_to", "owner_name", "org_name", "cc_email", "company_id", "label",
    "picture_id", "followers_count", "won_deals_count", "related_won_deals_count", "lost_deals_count",
    "related_lost_deals_count", "last_activity_id", "last_activity_date", "next_activity_id",
    "next_activity_date", "next_activity_time", "last_incoming_mail_time", "last_outgoing_mail_time",
    "email", "phone", "im", "primary_email", "marketing_status", "doi_status"}
PER_DELETE = 12          # 2 live reads (2 tokens each) + 1 delete (6) + slack


def has(v) -> bool:
    if v in (None, "", [], {}):
        return False
    if isinstance(v, list):
        return any((x.get("value") if isinstance(x, dict) else x) for x in v)
    if isinstance(v, dict):
        return has(v.get("value"))
    return bool(str(v).strip())


def owner_of(p) -> str:
    o = p.get("owner_id")
    return str(o.get("id") if isinstance(o, dict) else o or "")


def why_not_empty(copy: dict, kept: dict) -> str:
    """'' when the copy is a deletable empty Clay copy of `kept`, else the reason it is not."""
    if copy.get("active_flag") is False:
        return "already deleted"
    if kept.get("active_flag") is False:
        return "the kept record is deleted"
    if str(copy.get("id")) == str(kept.get("id")):
        return "copy and kept record are the same record"
    if owner_of(copy) != CLAY_ACTOR:
        return "not created by Clay"
    if (copy.get("add_time") or "") < EARLIEST:
        return "older than the Clay runs"
    if (copy.get("add_time") or "") <= (kept.get("add_time") or ""):
        return "the copy is older than the kept record"
    if has(copy.get("email")) or has(copy.get("phone")):
        return "has an email or phone"
    busy = [k for k in COUNTS if int(copy.get(k) or 0)]
    if busy:
        return "has " + ", ".join(k.replace("_count", "").replace("_", " ") for k in busy)
    if dp.org_of(copy) != dp.org_of(kept):
        return "at a different company than the kept record"
    if dp.names_agree(dp.Name(copy.get("name") or ""), dp.Name(kept.get("name") or "")) == 0:
        return "name differs from the kept record"
    more = [k for k, v in copy.items() if k not in NOT_DATA and has(v) and not has(kept.get(k))]
    if more:
        return "holds a field the kept record lacks (" + ", ".join(sorted(more))[:120] + ")"
    return ""


def read_rows(path: Path) -> list:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return [r for r in csv.DictReader(fh) if (r.get("loser_id") or "").isdigit()
                and (r.get("survivor_id") or "").isdigit()]


def ledger_ids(path: Path, key: str) -> set:
    out = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.add(int(json.loads(line).get(key)))
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
    return out


# ---- split ----------------------------------------------------------------------------------

def cmd_split(a, root: Path) -> int:
    import pd_cache
    rows = read_rows(Path(a.plan))
    P = {str(p["id"]): p for p in pd_cache.get_persons()}
    delete, merge, reasons = [], [], Counter()
    for r in rows:
        c, k = P.get(r["loser_id"]), P.get(r["survivor_id"])
        if not c or not k:
            reasons["not in the snapshot"] += 1
            continue
        why = why_not_empty(c, k)
        if why:
            reasons[why.split(" (")[0]] += 1
            merge.append({**r, "note": why})
        else:
            delete.append({**r, "match_on": "clay-t1-empty-copy", "note": "empty Clay copy - delete"})
    out = dc.out_dir(root, "persons")
    day = dc.today()
    dpath, mpath = out / f"clay_copies_delete_{day}.csv", out / f"clay_copies_merge_{day}.csv"
    dc.write_csv(dpath, dp.CSV_HEADER, delete)
    dc.write_csv(mpath, dp.CSV_HEADER, merge)
    log(json.dumps({"delete": len(delete), "merge": len(merge), "merge_reasons": dict(reasons)}, indent=2))
    print(f"RESULT: clay-copies split delete={len(delete)} merge={len(merge)} delete_csv={dpath} merge_csv={mpath}")
    return 0


# ---- delete ---------------------------------------------------------------------------------

def live(pd_call, pid) -> "dict | None":
    try:
        r = pd_call("GET", f"/persons/{pid}", api_version="v1")
    except RuntimeError as e:
        log(f"[cleanup] read {pid} failed: {str(e)[:200]}")
        return None
    return r.get("data") if isinstance(r, dict) and r.get("success") else None


def cmd_delete(a, root: Path) -> int:
    import pd_budget
    from pipedrive_create import pd_call
    rows = read_rows(Path(a.plan))
    base = root / dc.DEDUP_OUT_REL / "persons"
    out = dc.out_dir(root, "persons")
    ledger = base / "deleted_ledger.jsonl"
    audit = out / f"delete_audit_{dc.today()}.jsonl"
    gone = ledger_ids(ledger, "deleted") | ledger_ids(base / "merged_ledger.jsonl", "loser")
    res = {"deleted": [], "skipped": [], "failed": [], "stopped": ""}
    for r in rows:
        if a.limit is not None and len(res["deleted"]) >= a.limit:
            break
        cid, kid = int(r["loser_id"]), int(r["survivor_id"])
        if cid in gone:
            res["skipped"].append({"id": cid, "why": "already handled"})
            continue
        if not a.execute:
            res["deleted"].append({"id": cid, "kept": kid, "dry": True})
            continue
        ok, why = pd_budget.may_spend(PER_DELETE, fresh=len(res["deleted"]) % 25 == 0)
        if not ok:
            res["stopped"] = why
            break
        copy, kept = live(pd_call, cid), live(pd_call, kid)
        if copy is None or kept is None:
            res["skipped"].append({"id": cid, "why": "could not read the copy or the kept record"})
            continue
        why = why_not_empty(copy, kept)
        if why:
            res["skipped"].append({"id": cid, "kept": kid, "why": why})
            log(f"[cleanup] SKIP {cid}: {why}")
            continue
        with audit.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": _dt.datetime.now().isoformat(timespec="seconds"), "action": "delete",
                                 "copy": cid, "kept": kid, "copy_before": copy}, ensure_ascii=False, default=str) + "\n")
        try:
            d = pd_call("DELETE", f"/persons/{cid}", api_version="v1")
        except RuntimeError as e:
            d = {"success": False, "error": str(e)[:300]}
        if isinstance(d, dict) and d.get("success"):
            with ledger.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"deleted": cid, "kept": kid, "name": copy.get("name"),
                                     "ts": _dt.datetime.now().isoformat(timespec="seconds")}) + "\n")
            res["deleted"].append({"id": cid, "kept": kid, "name": copy.get("name"), "org": copy.get("org_name")})
            log(f"[cleanup] deleted copy {cid} ({copy.get('name')}), kept {kid}")
        else:
            res["failed"].append({"id": cid, "error": json.dumps(d)[:300]})
            log(f"[cleanup] FAILED {cid}: {json.dumps(d)[:200]}")
    (out / f"delete_results_{dc.today()}.json").write_text(json.dumps(res, indent=2, ensure_ascii=False, default=str),
                                                           encoding="utf-8")
    print(f"RESULT: clay-copies {'delete' if a.execute else 'DRY delete'} deleted={len(res['deleted'])} "
          f"skipped={len(res['skipped'])} failed={len(res['failed'])} stopped={res['stopped'] or 'no'}")
    return 3 if res["failed"] else 0


# ---- restore --------------------------------------------------------------------------------

def cmd_restore(a, root: Path) -> int:
    from pipedrive_create import pd_call
    before = live(pd_call, a.id)
    log(f"[cleanup] {a.id} before: active_flag={None if before is None else before.get('active_flag')} "
        f"name={None if before is None else before.get('name')}")
    if not a.execute:
        print("RESULT: restore dry-run (pass --execute)")
        return 0
    try:
        r = pd_call("PUT", f"/persons/{a.id}", body={"active_flag": True}, api_version="v1")
    except RuntimeError as e:
        r = {"success": False, "error": str(e)[:300]}
    after = live(pd_call, a.id)
    ok = bool(after and after.get("active_flag") is True)
    log(f"[cleanup] restore response success={isinstance(r, dict) and r.get('success')}; "
        f"after: active_flag={None if after is None else after.get('active_flag')}")
    if not ok:
        log(f"[cleanup] response: {json.dumps(r)[:400]}")
    else:
        # a restored copy is live again: drop it from the ledger so a later run can handle it
        ledger = root / dc.DEDUP_OUT_REL / "persons" / "deleted_ledger.jsonl"
        if ledger.exists():
            keep = [ln for ln in ledger.read_text(encoding="utf-8").splitlines()
                    if ln.strip() and str(json.loads(ln).get("deleted")) != str(a.id)]
            ledger.write_text("".join(ln + "\n" for ln in keep), encoding="utf-8")
    print(f"RESULT: restore id={a.id} restored={'yes' if ok else 'NO'}")
    return 0 if ok else 3


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--capy-root", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("split"); s.add_argument("--plan", required=True)
    s = sub.add_parser("delete"); s.add_argument("--plan", required=True)
    s.add_argument("--limit", type=int, default=None); s.add_argument("--execute", action="store_true")
    s = sub.add_parser("restore"); s.add_argument("--id", type=int, required=True)
    s.add_argument("--execute", action="store_true")
    a = ap.parse_args()
    root = dc.find_capy_root(a.capy_root, dp.SKILL)
    if root is None:
        log("ERROR: could not locate gocapy-claude-plugin. Pass --capy-root.")
        return 2
    dc.bootstrap(root)
    dp.NICK.update(dp.load_nicknames())
    return {"split": cmd_split, "delete": cmd_delete, "restore": cmd_restore}[a.cmd](a, root)


if __name__ == "__main__":
    raise SystemExit(main())

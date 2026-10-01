#!/usr/bin/env python3
"""Approval -> automatic merge for the weekly Pipedrive dedup (Marcella 2026-09-30).

The weekly report (dedup_orgs.py / dedup_persons.py --post-task) posts a label import file for the
SURE tier and records itself in shared-references/dedup/<orgs|persons>/reports.jsonl. Marcella
imports the labels, deletes copies by hand, then replies "merge them" on the dedup task. This
script, run DAILY by the scheduled runner:

  1. looks for a reply from Marcella on the task (top-level or in a thread) posted AFTER the
     newest report of each kind, saying merge / go ahead / yes, and not saying wait / hold / no;
  2. records the approval in shared-references/dedup/approvals.json and posts one short note;
  3. every day until finished, runs `--execute --plan <that report's csv> --tiers high` for each
     approved list (orgs first). The budget brake stops a night early and the merged ledger makes
     the next night pick up where it stopped; copies she already deleted are skipped as inactive;
  4. posts one closing note per list when it is done.

Only the high (sure) tier is ever merged here. Medium / review rows still need her explicit ask.

  python dedup_approval.py              # check for a reply + merge approved lists
  python dedup_approval.py --dry-run    # report what it would do, no merges, no comments
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dedup_common as dc  # noqa: E402

log = dc.log
TASK = "86bc8kj30"
MARCELLA = "32165284"
APPROVE = re.compile(r"\b(merge|go ahead|approved?|yes|do it)\b", re.IGNORECASE)
HOLD = re.compile(r"\b(don'?t|do not|hold|wait|stop|not yet|no)\b", re.IGNORECASE)
KINDS = {"orgs": ("companies", HERE / "dedup_orgs.py"),
         "persons": ("people", HERE.parent.parent / "pipedrive-person-dedup" / "scripts" / "dedup_persons.py")}


def cu(root: Path, method: str, path: str) -> dict:
    p = subprocess.run([sys.executable, str(root / dc.CLICKUP_REL / "clickup_call.py"), method, path],
                       capture_output=True, text=True, encoding="utf-8")
    if p.returncode != 0:
        raise RuntimeError(f"clickup {method} {path} failed: {p.stderr[-300:]}")
    return json.loads(p.stdout or "{}")


def note(root: Path, text: str, dry: bool) -> None:
    if dry:
        log(f"[dry-run] would post: {text}")
        return
    subprocess.run([sys.executable, str(root / dc.CLICKUP_REL / "kodie_notify.py"), "--task", TASK,
                    "--kind", "note", "--text", text], capture_output=True, text=True, encoding="utf-8")


def latest_report(base: Path, kind: str) -> "dict | None":
    f = base / kind / dc.REPORTS_FILE
    if not f.exists():
        return None
    lines = [ln for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return json.loads(lines[-1]) if lines else None


def epoch_ms(iso: str) -> int:
    return int(datetime.datetime.fromisoformat(iso).timestamp() * 1000)


def marcella_replies(root: Path, since_ms: int) -> list:
    """Marcella's comments on the task after `since_ms`, top-level and threaded, oldest first."""
    top = (cu(root, "GET", f"/task/{TASK}/comment").get("comments") or [])
    found = []
    for c in top:
        when = int(c.get("date") or 0)
        uid = str((c.get("user") or {}).get("id") or "")
        if when > since_ms and uid == MARCELLA:
            found.append(c)
        if int(c.get("reply_count") or 0) and when > since_ms - 15 * 60 * 1000:
            for r in cu(root, "GET", f"/comment/{c['id']}/reply").get("comments") or []:
                if int(r.get("date") or 0) > since_ms and str((r.get("user") or {}).get("id")) == MARCELLA:
                    found.append(r)
    return sorted(found, key=lambda c: int(c.get("date") or 0))


def is_go(text: str) -> bool:
    return bool(APPROVE.search(text or "")) and not HOLD.search(text or "")


def run_merge(root: Path, kind: str, plan: str, dry: bool) -> dict:
    script = KINDS[kind][1]
    if dry:
        log(f"[dry-run] would run {script.name} --execute --plan {plan} --tiers high")
        return {"dry_run": True}
    p = subprocess.run([sys.executable, str(script), "--execute", "--plan", plan, "--tiers", "high"],
                       capture_output=True, text=True, encoding="utf-8")
    sys.stderr.write(p.stderr[-4000:])
    if p.returncode == 2:
        return {"finished": True, "merged_count": 0, "skipped_count": 0, "failed_count": 0,
                "setup_error": p.stderr[-300:]}
    res_path = dc.out_dir(root, kind) / f"results_{dc.today()}.json"
    res = json.loads(res_path.read_text(encoding="utf-8")) if res_path.exists() else {}
    res["finished"] = not res.get("stopped")
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--capy-root", default=None)
    args = ap.parse_args()
    root = dc.find_capy_root(args.capy_root, HERE.parent)
    if root is None:
        log("ERROR: could not locate gocapy-claude-plugin. Pass --capy-root.")
        print("RESULT: dedup-approval setup error")
        return 2
    dc.bootstrap(root)
    base = root / dc.DEDUP_OUT_REL
    state_path = base / "approvals.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}

    # 1-2. new approvals
    for kind in KINDS:
        rep = latest_report(base, kind)
        if not rep:
            continue
        key = f"{kind}:{rep['day']}"
        if key in state:
            continue
        go = next((c for c in marcella_replies(root, epoch_ms(rep["ts"]))
                   if is_go(c.get("comment_text") or "")), None)
        if not go:
            log(f"[approval] {key}: no go-ahead from Marcella yet")
            continue
        state[key] = {"kind": kind, "plan": rep["plan"], "sure_rows": rep["sure_rows"],
                      "approved_comment": str(go.get("id")), "done": False,
                      "merged": 0, "skipped": 0, "failed": 0,
                      "approved_at": datetime.datetime.now().isoformat(timespec="seconds")}
        log(f"[approval] {key}: go-ahead found (comment {go.get('id')})")
        if not args.dry_run:  # persist before any merge so a crash can't re-post the note
            state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        note(root, f"Got it - merging the {rep['sure_rows']:,} {KINDS[kind][0]} duplicates I was sure "
                   f"about from this week's list, a batch each night. Copies you deleted are skipped.",
             args.dry_run)

    # 3-4. merge every approved, unfinished list
    ran = []
    for key, st in state.items():
        if st.get("done"):
            continue
        res = run_merge(root, st["kind"], st["plan"], args.dry_run)
        if res.get("dry_run"):
            continue
        for a, b in (("merged", "merged_count"), ("skipped", "skipped_count"), ("failed", "failed_count")):
            st[a] = st.get(a, 0) + int(res.get(b) or 0)
        ran.append(f"{key} merged={res.get('merged_count', 0)}")
        if res.get("finished"):
            st["done"] = True
            st["finished_at"] = datetime.datetime.now().isoformat(timespec="seconds")
            word = KINDS[st["kind"]][0]
            fail = f" {st['failed']} could not be merged - I'll look at those." if st["failed"] else ""
            note(root, f"Done merging this week's sure {word} duplicates: {st['merged']:,} merged into the "
                       f"record we keep, {st['skipped']:,} skipped (already deleted or gone).{fail}",
                 args.dry_run)

    if not args.dry_run:
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    open_lists = sum(1 for s in state.values() if not s.get("done"))
    print(f"RESULT: dedup-approval ran={len(ran)} open_lists={open_lists} {'; '.join(ran)}".rstrip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

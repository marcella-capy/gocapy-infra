#!/usr/bin/env python3
"""Daily driver: reconcile recently-completed call tasks and load those people into HotHawk.

This replaces the claude.ai "Call Task Reconcile" cloud routine, which failed SILENTLY three
times (dead since ~2026-07-10; missed 7/14's 65 done calls; zero loads 7/28-7/30) because
nothing local ever saw its exit code and it left no artifact to notice the absence of. Same two
scripts a human runs by hand, on the local scheduler, where a failure lands in a log, leaves an
artifact, and trips the Discord alert.

  1. reconcile_done_calls.py --since <N business days back>   -> _reconcile_<start>-<end>.json
     The lookback is what makes the run self-healing: a day that fails gets retried by the next
     N runs, and _reconcile_state.json keeps it idempotent so nobody is subscribed twice.
  2. hothawk_subscribe.py --artifact <that file> --apply
     Sibling call tasks are LEFT OPEN (Marcella 2026-07-15) — this driver never passes
     --close-siblings.

Exit codes: 0 = ok (including "nothing to subscribe"), 1 = a step failed or the HotHawk push
came back `RESULT: partial` (a principal's campaign is broken — leads are on the list but those
people were deliberately NOT marked subscribed, so the next run retries them).

CLI:
    python reconcile_and_load.py [--lookback 3] [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import subprocess
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).resolve().parent


def _business_days_back(d: _dt.date, n: int) -> _dt.date:
    """n business days before d (weekends only, no holiday calendar — matches business_days.py)."""
    out = d
    while n > 0:
        out -= _dt.timedelta(days=1)
        if out.weekday() < 5:
            n -= 1
    return out


def _run(script: str, args: list) -> "tuple[int, str]":
    cmd = [sys.executable, str(HERE / script)] + args
    print(f"$ {' '.join(cmd[1:])}", flush=True)
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (p.stdout or "") + (p.stderr or "")
    print(out, flush=True)
    return p.returncode, out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lookback", type=int, default=3,
                    help="business days to look back (default 3; self-heals failed days)")
    ap.add_argument("--dry-run", action="store_true",
                    help="reconcile + plan the HotHawk push without writing anything")
    a = ap.parse_args()

    since = _business_days_back(_dt.date.today(), a.lookback)
    rc, out = _run("reconcile_done_calls.py", ["--since", since.isoformat()])
    if rc != 0:
        print(f"RESULT: error - reconcile_done_calls.py exited {rc}")
        return 1

    m = re.search(r"->\s+(_reconcile_[\w.-]+\.json)", out)
    if not m:
        print("RESULT: error - could not find the reconcile artifact name in the output")
        return 1
    artifact = HERE / m.group(1)
    try:
        entries = json.loads(artifact.read_text(encoding="utf-8-sig")).get("to_subscribe", [])
    except (OSError, json.JSONDecodeError) as e:
        print(f"RESULT: error - unreadable artifact {artifact.name}: {e}")
        return 1

    if not entries:
        print(f"RESULT: ok - nothing to subscribe since {since} ({artifact.name})")
        return 0

    push = ["--artifact", artifact.name] + ([] if a.dry_run else ["--apply"])
    rc, out = _run("hothawk_subscribe.py", push)
    if rc != 0:
        print(f"RESULT: error - hothawk_subscribe.py exited {rc}")
        return 1
    if "RESULT: partial" in out:
        print(f"RESULT: error - HotHawk push partial ({len(entries)} planned) — see the failed "
              f"groups above; those people stay unmarked and retry next run")
        return 1

    print(f"RESULT: ok - {len(entries)} people loaded into HotHawk since {since} "
          f"({artifact.name}){' [DRY-RUN]' if a.dry_run else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

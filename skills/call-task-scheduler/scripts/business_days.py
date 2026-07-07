#!/usr/bin/env python3
"""Business-day date math for call-task-scheduler. Weekends only — no holiday calendar.

API:
    add_business_days(d, n)  -> date   # n business days after d (n >= 1); lands on a weekday
    previous_business_day(d) -> date   # nearest weekday strictly before d

CLI:
    python business_days.py --self-test
"""
from __future__ import annotations

import datetime as _dt


def add_business_days(d: _dt.date, n: int) -> _dt.date:
    out = d
    while n > 0:
        out += _dt.timedelta(days=1)
        if out.weekday() < 5:
            n -= 1
    return out


def previous_business_day(d: _dt.date) -> _dt.date:
    out = d - _dt.timedelta(days=1)
    while out.weekday() >= 5:
        out -= _dt.timedelta(days=1)
    return out


def _self_test() -> int:
    fri = _dt.date(2026, 7, 3)   # Friday
    tue = _dt.date(2026, 7, 7)   # Tuesday
    cases = [
        (add_business_days(fri, 1), _dt.date(2026, 7, 6)),   # Fri +1 -> Mon
        (add_business_days(fri, 5), _dt.date(2026, 7, 10)),  # Fri +5 -> next Fri
        (add_business_days(fri, 7), _dt.date(2026, 7, 14)),  # Fri +7 -> Tue after
        (add_business_days(tue, 1), _dt.date(2026, 7, 8)),
        (add_business_days(tue, 5), _dt.date(2026, 7, 14)),
        (add_business_days(tue, 7), _dt.date(2026, 7, 16)),
        (previous_business_day(_dt.date(2026, 7, 6)), fri),  # Mon -> prior Fri
        (previous_business_day(tue), _dt.date(2026, 7, 6)),
    ]
    for got, want in cases:
        if got != want:
            print(f"RESULT: error - self-test failed: got {got}, want {want}")
            return 1
    print(f"RESULT: ok - {len(cases)} self-test cases passed")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_self_test() if "--self-test" in sys.argv else _self_test())

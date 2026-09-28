#!/usr/bin/env python3
"""Shared plumbing for the two whole-database dedup skills (pipedrive-org-dedup and
pipedrive-person-dedup). Nothing here decides WHAT is a duplicate — each runner owns its own
match rules. This module owns the parts that must behave identically for both:

  - locating the gocapy-claude-plugin marketplace and putting its primitives on sys.path
  - the fixed output folder  go-capy-outreach/shared-references/dedup/<kind>/<YYYYMMDD>/
  - text normalization (accents, legal suffixes, LinkedIn slugs) and a tiny union-find
  - the reviewed-plan CSV contract (tiers) that --execute consumes
  - the MERGE executor: budget brake -> pre-merge snapshot (audit) -> merge -> post-merge
    repair of comma-concatenated text fields. It NEVER writes a name or a phone (golden rule 10).

Pipedrive merge semantics we have seen live (memory: pipedrive-org-merge-concatenates-string-fields,
lamresearch-...-person-merge-concat-bug): the `merge_with_id` record keeps its id, blanks fill from
the loser, but a text field set on BOTH records comes back comma-joined ("ok, valid", doubled
LinkedIn URL, "Yes, No"). A merge cannot be undone, so the survivor's pre-merge values are written
to an audit JSONL before every merge and any joined field is PATCHed back to the survivor's value.
"""
from __future__ import annotations

import csv
import datetime
import json
import re
import sys
import time
import unicodedata
import urllib.parse
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

PD_CACHE_REL = Path("gocapy-claude-plugin/go-capy-outreach/scripts")
FINDPEOPLE_REL = Path("gocapy-claude-plugin/go-capy-outreach/skills/find-people-at-organizations/scripts")
CLICKUP_REL = Path("gocapy-claude-plugin/go-capy-outreach/skills/clickup/scripts")
DEDUP_OUT_REL = Path("gocapy-claude-plugin/go-capy-outreach/shared-references/dedup")

TIERS = ("high", "medium", "review", "name-only")

# Never written by the repair step, whatever the merge did to them (golden rule 10 + arrays).
NEVER_WRITE = {"name", "first_name", "last_name", "phone", "email"}
_HASH_KEY = re.compile(r"^[0-9a-f]{40}$")
# Built-in text fields that can come back joined on a merge and are safe to restore.
REPAIRABLE_BUILTINS = {"website", "linkedin", "industry", "job_title"}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def today() -> str:
    return datetime.date.today().strftime("%Y%m%d")


# -- locating the marketplace ---------------------------------------------------------------

def find_capy_root(explicit: "str | None", start: Path) -> "Path | None":
    """The marketplaces dir that holds gocapy-claude-plugin. Walk up from `start`, then glob."""
    if explicit:
        p = Path(explicit)
        return p if (p / PD_CACHE_REL / "pd_cache.py").exists() else None
    for base in [start, *start.parents]:
        if (base / PD_CACHE_REL / "pd_cache.py").exists():
            return base
    for base in start.parents:
        for cand in base.glob("**/gocapy-claude-plugin/go-capy-outreach/scripts/pd_cache.py"):
            return cand.parents[3]
    return None


def bootstrap(root: Path) -> None:
    for rel in (PD_CACHE_REL, FINDPEOPLE_REL):
        p = str(root / rel)
        if p not in sys.path:
            sys.path.insert(0, p)


def out_dir(root: Path, kind: str, day: "str | None" = None) -> Path:
    d = root / DEDUP_OUT_REL / kind / (day or today())
    d.mkdir(parents=True, exist_ok=True)
    return d


# -- normalization --------------------------------------------------------------------------

def ascii_fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def linkedin_slug(url, kind: str) -> str:
    """Stable LinkedIn key. kind='company' -> /company|school|showcase/<slug>; kind='in' -> /in/<slug>.
    Tolerates scheme/locale subdomain/www/query/trailing slash and %-encoding. '' when absent."""
    if not url or not isinstance(url, str):
        return ""
    u = urllib.parse.unquote(url.strip()).lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^([a-z]{2,3}\.)?(www\.)?linkedin\.com", "linkedin.com", u)
    u = u.split("?")[0].split("#")[0].rstrip("/")
    pat = r"linkedin\.com/(?:company|school|showcase)/([^/]+)" if kind == "company" \
        else r"linkedin\.com/in/([^/]+)"
    m = re.search(pat, u)
    return m.group(1).strip() if m else ""


class UnionFind:
    def __init__(self):
        self.parent: dict = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)

    def groups(self) -> dict:
        out: dict = {}
        for x in list(self.parent):
            out.setdefault(self.find(x), []).append(x)
        return out


def jaro_winkler(a: str, b: str) -> float:
    if a == b:
        return 1.0
    la, lb = len(a), len(b)
    if not la or not lb:
        return 0.0
    rng = max(la, lb) // 2 - 1
    am, bm = [False] * la, [False] * lb
    m = 0
    for i, ch in enumerate(a):
        for j in range(max(0, i - rng), min(lb, i + rng + 1)):
            if not bm[j] and b[j] == ch:
                am[i] = bm[j] = True
                m += 1
                break
    if not m:
        return 0.0
    t, k = 0, 0
    for i in range(la):
        if am[i]:
            while not bm[k]:
                k += 1
            if a[i] != b[k]:
                t += 1
            k += 1
    jaro = (m / la + m / lb + (m - t / 2) / m) / 3
    p = 0
    for x, y in zip(a[:4], b[:4]):
        if x != y:
            break
        p += 1
    return jaro + p * 0.1 * (1 - jaro)


# -- plan CSV contract ----------------------------------------------------------------------

def write_csv(path: Path, header: list, rows: list) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=header, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def read_plan(path: Path, tiers: set) -> list:
    """Approved loser->survivor rows, filtered to the tiers the human approved. --execute acts
    ONLY on these; it never re-derives the plan."""
    rows = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            try:
                tier = (r.get("tier") or "").strip().lower()
                if tier not in tiers:
                    continue
                rows.append({**r, "tier": tier,
                             "survivor": int(r["survivor_id"]), "loser": int(r["loser_id"])})
            except (KeyError, ValueError, TypeError):
                continue
    return rows


def parse_tiers(raw: "str | None") -> "set | None":
    if not raw:
        return None
    ts = {t.strip().lower() for t in raw.split(",") if t.strip()}
    if "all" in ts:
        return set(TIERS)
    bad = ts - set(TIERS)
    if bad:
        raise SystemExit(f"ERROR: unknown tier(s) {sorted(bad)}; choose from {TIERS} or 'all'")
    return ts


# -- merge executor -------------------------------------------------------------------------

def _repairable(key: str) -> bool:
    return key not in NEVER_WRITE and (bool(_HASH_KEY.match(key)) or key in REPAIRABLE_BUILTINS)


def _text_fields(rec: dict) -> dict:
    return {k: v for k, v in (rec or {}).items()
            if _repairable(k) and isinstance(v, str) and v.strip()}


def joined_fields(before: dict, after: dict) -> dict:
    """{key: pre-merge value} for every survivor text field the merge changed by JOINING
    (the old value is still inside the new one). Blank-before fields are fills, left alone."""
    fix = {}
    for k, bv in _text_fields(before).items():
        av = (after or {}).get(k)
        if isinstance(av, str) and av != bv and bv in av:
            fix[k] = bv
    return fix


def _get(pd_call, entity: str, rid: int) -> "dict | None":
    resp = pd_call("GET", f"/{entity}/{rid}", api_version="v1")
    if isinstance(resp, dict) and resp.get("success") and isinstance(resp.get("data"), dict):
        return resp["data"]
    return None


class Budget:
    """Pipedrive daily-token brake (pd_budget keeps the last 15%). pd_call does not surface
    response headers, so re-probe (2 tokens) every `every` merges to keep the reading current."""

    def __init__(self, per_merge: int = 40, every: int = 25):
        import pd_budget  # noqa: E402 — on sys.path after bootstrap()
        self.pd_budget, self.per_merge, self.every, self.n = pd_budget, per_merge, every, 0

    def ok(self) -> "tuple[bool, str]":
        fresh = self.n % self.every == 0
        self.n += 1
        return self.pd_budget.may_spend(self.per_merge, fresh=fresh)


def load_merged_ledger(path: Path) -> set:
    done = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                done.add(int(json.loads(line)["loser"]))
            except (ValueError, KeyError, TypeError):
                continue
    return done


def execute_merges(rows: list, *, entity: str, merge_fn, pd_call, out: Path, ledger: Path,
                   limit: "int | None", key_label: str = "match_on") -> dict:
    """Merge each approved row (loser -> survivor) with a budget brake, a pre-merge audit line and
    post-merge repair of joined text fields. Skips losers already merged (ledger) or gone.
    Returns a results dict; never raises on a single failed row."""
    budget = Budget()
    done = load_merged_ledger(ledger)
    audit_path = out / f"audit_{today()}.jsonl"
    merged, failed, skipped = [], [], []
    stop_reason = ""
    count = 0
    for r in rows:
        if limit is not None and count >= limit:
            break
        loser, survivor = r["loser"], r["survivor"]
        if loser in done:
            skipped.append({"loser": loser, "survivor": survivor, "why": "already merged (ledger)"})
            continue
        allowed, why = budget.ok()
        if not allowed:
            stop_reason = why
            log(f"[dedup] STOP — {why}. Re-run tomorrow with the same --plan; merged rows are skipped.")
            break
        before = _get(pd_call, entity, survivor)
        lrec = _get(pd_call, entity, loser)
        if before is None or lrec is None or lrec.get("active_flag") is False:
            skipped.append({"loser": loser, "survivor": survivor,
                            "why": "survivor or loser not found / inactive"})
            continue
        count += 1
        with audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": datetime.datetime.now().isoformat(timespec="seconds"),
                                 "entity": entity, "loser": loser, "survivor": survivor,
                                 "tier": r.get("tier"), "match": r.get(key_label),
                                 "survivor_before": before, "loser_before": lrec},
                                ensure_ascii=False, default=str) + "\n")
        try:
            status, resp = merge_fn(loser, survivor, dry_run=False)
        except Exception as exc:  # noqa: BLE001 — keep the batch going
            status, resp = "error", {"exception": str(exc)}
        if status != "merged":
            log(f"[{entity}-merge-FAILED] {loser} -> {survivor}: {str(resp)[:300]}")
            failed.append({"loser": loser, "survivor": survivor, "response": resp})
            continue
        after = _get(pd_call, entity, survivor) or {}
        fix = joined_fields(before, after)
        repaired = {}
        if fix:
            assert not (set(fix) & NEVER_WRITE), "repair would touch a name/phone field"
            pr = pd_call("PUT", f"/{entity}/{survivor}", body=fix, api_version="v1")
            ok = isinstance(pr, dict) and pr.get("success") is not False
            repaired = {"fields": sorted(fix), "ok": ok}
        log(f"[{entity}-merge] {loser} -> {survivor} ({r.get('tier')})"
            + (f" repaired {len(fix)} joined field(s)" if fix else ""))
        entry = {"loser": loser, "survivor": survivor, "tier": r.get("tier"), "repaired": repaired}
        merged.append(entry)
        with ledger.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({**entry, "ts": datetime.datetime.now().isoformat(timespec="seconds")})
                     + "\n")
        time.sleep(0.2)
    return {"approved_rows": len(rows), "merged_count": len(merged), "failed_count": len(failed),
            "skipped_count": len(skipped), "stopped": stop_reason, "merged": merged,
            "failed": failed, "skipped": skipped, "audit": str(audit_path)}


# -- ClickUp posting (Kodie) ----------------------------------------------------------------

def post_to_clickup(root: Path, task_id: str, text: str, attachments: list) -> bool:
    """Attach the review files and post a plain-English note as Kodie (kind=note)."""
    import subprocess
    scripts = root / CLICKUP_REL
    ok = True
    for f in attachments:
        p = subprocess.run([sys.executable, str(scripts / "clickup_call.py"), "attach", task_id, str(f)],
                           capture_output=True, text=True, encoding="utf-8")
        if p.returncode != 0:
            ok = False
            log(f"[clickup] attach failed for {f}: {p.stderr[-400:]}")
    p = subprocess.run([sys.executable, str(scripts / "kodie_notify.py"), "--task", task_id,
                        "--kind", "note", "--text", text],
                       capture_output=True, text=True, encoding="utf-8")
    if p.returncode != 0:
        ok = False
        log(f"[clickup] comment failed: {p.stderr[-400:]}")
    return ok

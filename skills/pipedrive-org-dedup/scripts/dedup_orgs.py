#!/usr/bin/env python3
"""pipedrive-org-dedup skill — whole-database duplicate-organization merge.

Finds organizations that are the same company and merges the duplicates into one survivor.
Three match rules, each tagged on every CSV row (the `match_on` column):
  - domain    same normalized website domain (seed_resolver.normalize_domain; blocklist applies)
  - linkedin  same LinkedIn company page (built-in org field `linkedin`, /company|school|showcase/<slug>)
  - name      same cleaned name (accents off, Inc/LLC/Corp/Co/Ltd/Group/Holdings stripped)

Records linked by domain OR LinkedIn form one group (union-find). Each loser row gets a tier:
  high       the SAME name (legal suffix/punctuation/typo aside) AND domain or LinkedIn matches
  medium     one name is the other plus extra words ('Diebold' / 'Diebold Nixdorf') AND the
             website domain matches
  review     names differ (parent/subsidiary, e.g. Amazon Robotics vs Amazon Kuiper on
             aboutamazon.com, or a wrong LinkedIn link), LinkedIn is the only match and the names
             differ slightly (divisions often use the parent's page), a different non-blank Holding
             Co, a group of 6+, or a loser tied to the survivor only through a third record
  Sharing just a brand word ('Teledyne X' / 'Teledyne Y') never counts as the names agreeing.
  name-only  same cleaned name but no shared domain/LinkedIn — review list, never merged unless the
             human explicitly passes --tiers name-only on an approved file

Survivor: non-empty Company Research -> most people -> most activity/deals -> lowest id.
Guards: domain blocklist, protected names (whole group skipped), protected id pairs (never merged
together). Reads the daily snapshot (pd_cache) — zero Pipedrive calls on a dry run.

  python dedup_orgs.py                                   # dry run -> shared-references/dedup/orgs/<today>/
  python dedup_orgs.py --post-task 86bc8kj30             # ...and post the report + CSV to ClickUp as Kodie
  python dedup_orgs.py --execute --plan <csv> --tiers high [--limit 25]
  python dedup_orgs.py --execute --plan <edited csv> --tiers medium,review

--execute REQUIRES --plan (a human-approved CSV) and --tiers. It re-derives nothing. Every merge:
budget brake -> survivor snapshot to audit_<date>.jsonl -> MERGE -> joined text fields restored.
Name and phone are never written (golden rule 10). Merged losers are logged to merged_ledger.jsonl
and skipped on re-runs, so an interrupted batch simply resumes.

Exit codes: 0 ok, 2 setup/plan problem, 3 merge failures during --execute.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
REFERENCES = SKILL / "references"
sys.path.insert(0, str(HERE))
import dedup_common as dc  # noqa: E402

log = dc.log

LEGAL = {"inc", "incorporated", "llc", "l l c", "ltd", "limited", "corp", "corporation", "co",
         "company", "plc", "gmbh", "lp", "llp", "the", "group", "holdings", "holding", "sa", "ag",
         "bv", "srl", "pty", "pvt", "private", "lc", "pllc", "usa", "us"}
# Words too generic to prove two names are the same company.
GENERIC = LEGAL | {"and", "of", "industries", "industry", "industrial", "manufacturing", "mfg",
                   "technologies", "technology", "tech", "systems", "system", "international",
                   "intl", "america", "american", "national", "global", "enterprises",
                   "enterprise", "services", "service", "solutions", "products", "product",
                   "engineering", "precision", "machine", "machining", "aerospace", "defense",
                   "electronics", "components", "division", "div", "north", "south", "east", "west",
                   "united", "states", "de", "la", "del", "a", "an", "for", "at", "on", "in"}
MAX_GROUP = 5


def clean_name(name: str) -> str:
    n = dc.ascii_fold(name).replace("&", " and ")
    n = re.sub(r"\(.*?\)|\[.*?\]", " ", n)  # "(OH)", "(WA)" location tags
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    return " ".join(t for t in n.split() if t not in LEGAL)


def core_words(name: str) -> set:
    return {t for t in clean_name(name).split() if t not in GENERIC and len(t) > 1}


def name_strength(a: str, b: str) -> int:
    """2 = the same name (punctuation/spacing/legal suffix/generic words aside, or a small typo:
    'Earle M. Jorgensen' / 'Earle M Jorgenson'). 1 = one name is the other plus extra words
    ('Airbus' / 'Airbus Helicopter', 'Diebold' / 'Diebold Nixdorf') - often a division, so never
    'high'. 0 = different names. Sharing only a brand word is 0: 'Teledyne Reynolds' vs
    'Teledyne Qioptiq' are sister divisions, not duplicates."""
    ca, cb = clean_name(a), clean_name(b)
    if not ca or not cb:
        return 0
    na, nb = ca.replace(" ", ""), cb.replace(" ", "")
    wa, wb = core_words(a), core_words(b)
    if na == nb or (wa and wa == wb):
        return 2
    if min(len(na), len(nb)) >= 8 and dc.jaro_winkler(na, nb) >= 0.93:
        return 2
    short, long_ = sorted((na, nb), key=len)
    if (len(short) >= 3 and long_.startswith(short)) or (len(short) >= 5 and short in long_):
        return 1
    return 1 if (wa and wb and (wa <= wb or wb <= wa)) else 0


def names_similar(a: str, b: str) -> bool:
    return name_strength(a, b) > 0


def load_json_list(fname: str, key: str) -> list:
    p = REFERENCES / fname
    if not p.exists():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    items = data.get(key, []) if isinstance(data, dict) else data
    return [x for x in items if not (isinstance(x, str) and x.startswith("_"))]


def as_int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def activity(o: dict) -> int:
    return sum(as_int(o.get(k)) for k in ("activities_count", "open_deals_count", "closed_deals_count",
                                          "email_messages_count", "notes_count"))


class Ctx:
    def __init__(self, normalize, research_key, holding_key, blocklist, exclusions, pairs):
        self.normalize, self.research_key, self.holding_key = normalize, research_key, holding_key
        self.blocklist, self.exclusions = blocklist, [e.lower() for e in exclusions]
        self.pairs = {frozenset((int(a), int(b))) for a, b in pairs}

    def domain(self, o):
        d = self.normalize(o.get("website") or "")
        return "" if not d or d in self.blocklist else d

    def research(self, o):
        return bool(self.research_key and str(o.get(self.research_key) or "").strip())

    def holding(self, o):
        v = o.get(self.holding_key) if self.holding_key else None
        return clean_name(str(v)) if v else ""

    def excluded(self, o):
        n = (o.get("name") or "").lower()
        return any(e in n for e in self.exclusions)


def pick_survivor(group, ctx):
    return sorted(group, key=lambda o: (not ctx.research(o), -as_int(o.get("people_count")),
                                        -activity(o), int(o["id"])))[0]


def survivor_reason(s, ctx):
    if ctx.research(s):
        return "has Company Research"
    if as_int(s.get("people_count")):
        return f"most people ({as_int(s.get('people_count'))})"
    if activity(s):
        return "most activity"
    return "lowest id"


def build_plan(orgs: dict, ctx: Ctx):
    recs = {int(o["id"]): o for o in orgs.values() if o.get("active_flag", True) is not False}
    by = {"domain": defaultdict(set), "linkedin": defaultdict(set), "name": defaultdict(set)}
    stats = Counter(orgs_scanned=len(recs))
    for i, o in recs.items():
        raw = ctx.normalize(o.get("website") or "")
        if not raw:
            stats["no_domain"] += 1
        elif raw in ctx.blocklist:
            stats["blocklisted_domain"] += 1
        d = ctx.domain(o)
        li = dc.linkedin_slug(o.get("linkedin"), "company")
        nm = clean_name(o.get("name") or "")
        if d:
            by["domain"][d].add(i)
        if li:
            by["linkedin"][li].add(i)
        if len(nm.replace(" ", "")) >= 4:
            by["name"][nm].add(i)

    # Per-category raw counts (what Marcella asked for: duplicates by domain / LinkedIn / name).
    category = {}
    for k, idx in by.items():
        grps = [s for s in idx.values() if len(s) > 1]
        category[k] = {"groups": len(grps), "extra_records": sum(len(s) - 1 for s in grps)}

    uf = dc.UnionFind()
    for k in ("domain", "linkedin"):
        for ids in idx_groups(by[k]):
            for x in ids[1:]:
                uf.union(ids[0], x)

    rows, blocked, excluded_groups = [], [], []
    clustered = set()
    for members in uf.groups().values():
        if len(members) < 2:
            continue
        group = [recs[i] for i in members]
        clustered.update(members)
        if any(ctx.excluded(o) for o in group):
            excluded_groups.append(sorted(members))
            continue
        s = pick_survivor(group, ctx)
        sid = int(s["id"])
        for o in group:
            lid = int(o["id"])
            if lid == sid:
                continue
            if frozenset((lid, sid)) in ctx.pairs:
                blocked.append({"loser_id": lid, "survivor_id": sid, "why": "protected pair"})
                continue
            sig = []
            if ctx.domain(o) and ctx.domain(o) == ctx.domain(s):
                sig.append("domain")
            ls, lo = dc.linkedin_slug(s.get("linkedin"), "company"), dc.linkedin_slug(o.get("linkedin"), "company")
            if ls and ls == lo:
                sig.append("linkedin")
            same_name = clean_name(o.get("name") or "") == clean_name(s.get("name") or "")
            strength = name_strength(o.get("name") or "", s.get("name") or "")
            notes = []
            if not sig:
                tier = "review"
                notes.append("linked only through another record in the group")
            elif strength == 0:
                tier = "review"
                notes.append("names differ - possible parent/subsidiary or wrong LinkedIn")
            elif strength == 2:
                tier = "high"
            elif "domain" in sig:
                tier = "medium"
                notes.append("one name has extra words - check it is not a division")
            else:
                tier = "review"
                notes.append("same LinkedIn page only and names differ slightly - may be a division")
            hs, ho = ctx.holding(s), ctx.holding(o)
            if hs and ho and hs != ho:
                tier = "review"
                notes.append("different Holding Co")
            if len(group) > MAX_GROUP:
                tier = "review"
                notes.append(f"large group ({len(group)} records)")
            if same_name:
                sig.append("name")
            rows.append(row(tier, sig, s, o, ctx, "; ".join(notes), group_key(s, ctx)))

    # Name-only review list: same cleaned name, not already grouped by domain/LinkedIn together.
    for nm, ids in by["name"].items():
        if len(ids) < 2:
            continue
        roots = {uf.find(i) if i in clustered else i for i in ids}
        if len(roots) < 2:
            continue
        group = [recs[i] for i in ids]
        if any(ctx.excluded(o) for o in group):
            continue
        s = pick_survivor(group, ctx)
        sroot = uf.find(int(s["id"])) if int(s["id"]) in clustered else int(s["id"])
        for o in group:
            lid = int(o["id"])
            if lid == int(s["id"]) or (lid in clustered and uf.find(lid) == sroot):
                continue
            if frozenset((lid, int(s["id"]))) in ctx.pairs:
                continue
            note = "same name only - check websites/locations"
            d1, d2 = ctx.domain(s), ctx.domain(o)
            if d1 and d2 and d1 != d2:
                note = f"same name but different websites ({d1} vs {d2})"
            rows.append(row("name-only", ["name"], s, o, ctx, note, nm))

    order = {t: i for i, t in enumerate(dc.TIERS)}
    rows.sort(key=lambda r: (order[r["tier"]], r["group"], int(r["loser_id"])))
    tiers = Counter(r["tier"] for r in rows)
    stats.update({
        "groups_merged": len({r["survivor_id"] for r in rows if r["tier"] != "name-only"}),
        "excluded_groups_skipped": len(excluded_groups),
        "protected_pairs_blocked": len(blocked),
    })
    return rows, {"stats": dict(stats), "category": category, "tiers": dict(tiers),
                  "excluded_groups": excluded_groups, "blocked": blocked}


def idx_groups(index):
    return [sorted(s) for s in index.values() if len(s) > 1]


def group_key(s, ctx):
    return ctx.domain(s) or dc.linkedin_slug(s.get("linkedin"), "company") or clean_name(s.get("name") or "")


def row(tier, sig, s, o, ctx, note, group):
    return {"tier": tier, "match_on": "+".join(sig) or "-", "group": group,
            "survivor_id": int(s["id"]), "survivor_name": s.get("name"),
            "survivor_website": s.get("website") or "", "survivor_reason": survivor_reason(s, ctx),
            "loser_id": int(o["id"]), "loser_name": o.get("name"), "loser_website": o.get("website") or "",
            "loser_people": as_int(o.get("people_count")), "note": note}


CSV_HEADER = ["tier", "match_on", "group", "survivor_id", "survivor_name", "survivor_website",
              "survivor_reason", "loser_id", "loser_name", "loser_website", "loser_people", "note"]


def summary_text(summary, rows, csv_name, label_text) -> str:
    """Plain-English ClickUp note (Marcella is not a developer: outcome first, no jargon)."""
    t, st = summary["tiers"], summary["stats"]
    return (
        f"Weekly company duplicate check - nothing has been merged yet.\n\n"
        f"I checked {st['orgs_scanned']:,} companies. {t.get('high', 0)} records are copies I'm sure "
        f"about: same name, plus the same website or LinkedIn page.\n\n"
        f"{label_text}\n\n"
        f"When you're done deleting, reply \"merge them\" here and I'll merge the rest of that sure list "
        f"into the company we keep - people, deals and notes move over. Copies you already deleted are "
        f"skipped.\n\n"
        f"Left alone, not labelled: {t.get('medium', 0)} likely, {t.get('review', 0)} that need a human "
        f"look (possible parent/division pairs) and {t.get('name-only', 0)} that only share a name (full "
        f"list: {csv_name}). Protected companies (Lockheed, Northrop, Raytheon, Safran and pairs you "
        f"blocked) were skipped."
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true", help="Merge rows from an approved --plan CSV.")
    ap.add_argument("--plan", default=None, help="Human-approved CSV (required with --execute).")
    ap.add_argument("--tiers", default=None,
                    help="Comma list of approved tiers to execute: high,medium,review,name-only or all.")
    ap.add_argument("--limit", type=int, default=None, help="Cap merges this run (pilot batch).")
    ap.add_argument("--post-task", default=None, help="ClickUp task id: post the dry-run report there as Kodie.")
    ap.add_argument("--capy-root", default=None, help="Override autodetect of the marketplaces dir.")
    args = ap.parse_args()

    root = dc.find_capy_root(args.capy_root, SKILL)
    if root is None:
        log("ERROR: could not locate gocapy-claude-plugin/.../pd_cache.py. Pass --capy-root.")
        return 2
    dc.bootstrap(root)
    base = root / dc.DEDUP_OUT_REL / "orgs"

    if args.execute:
        tiers = dc.parse_tiers(args.tiers)
        if not args.plan or not tiers:
            log("ERROR: --execute needs --plan <approved csv> AND --tiers <approved tiers>. Refusing.")
            return 2
        plan = Path(args.plan)
        if not plan.exists():
            log(f"ERROR: plan not found: {plan}")
            return 2
        from pipedrive_create import merge_orgs, pd_call
        rows = dc.read_plan(plan, tiers)
        if not rows:
            log(f"ERROR: no rows in {plan.name} for tiers {sorted(tiers)}.")
            return 2
        out = dc.out_dir(root, "orgs")
        log(f"[dedup] EXECUTING up to {args.limit or len(rows)} of {len(rows)} approved rows "
            f"({sorted(tiers)}) from {plan.name} — MERGE only, nothing deleted.")
        res = dc.execute_merges(rows, entity="organizations", merge_fn=merge_orgs, pd_call=pd_call,
                                out=out, ledger=base / "merged_ledger.jsonl", limit=args.limit)
        res.update({"plan": str(plan), "tiers": sorted(tiers)})
        rp = out / f"results_{dc.today()}.json"
        rp.write_text(json.dumps(res, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        log(f"[dedup] DONE — merged {res['merged_count']}, failed {res['failed_count']}, "
            f"skipped {res['skipped_count']}{' (stopped: ' + res['stopped'] + ')' if res['stopped'] else ''}. "
            f"Results -> {rp}")
        print(f"RESULT: merged={res['merged_count']} failed={res['failed_count']} skipped={res['skipped_count']}")
        return 3 if res["failed"] else 0

    import pd_cache
    from seed_resolver import normalize_domain
    orgs = pd_cache.get_orgs()
    (_, _), (org_by_key, org_by_name) = pd_cache.get_field_maps()
    ctx = Ctx(normalize_domain,
              (org_by_name.get("Company Research") or {}).get("key"),
              (org_by_name.get("Holding Co") or {}).get("key"),
              {str(d).strip().lower() for d in load_json_list("dedup-domain-blocklist.json", "domains")},
              [str(n) for n in load_json_list("dedup-org-exclusions.json", "names")],
              [p for p in load_json_list("dedup-protected-pairs.json", "pairs") if isinstance(p, list)])
    rows, summary = build_plan(orgs, ctx)
    out = dc.out_dir(root, "orgs")
    csv_path = out / f"org_dedup_review_{dc.today()}.csv"
    label_path = out / f"org_dedup_label_import_{dc.today()}.csv"
    dc.write_csv(csv_path, CSV_HEADER, rows)
    recs = orgs.values() if isinstance(orgs, dict) else orgs
    by_id = {int(o["id"]): o for o in recs if isinstance(o, dict) and o.get("id") is not None}
    label_info = dc.write_label_csv(label_path, rows, by_id, org_by_key, "Organization")
    summary["label_file"] = label_info
    (out / f"org_dedup_summary_{dc.today()}.json").write_text(
        json.dumps({"date": dc.today(), "snapshot": pd_cache.snapshot_date("orgs"), **summary},
                   indent=2, ensure_ascii=False), encoding="utf-8")
    text = summary_text(summary, rows, csv_path.name,
                        dc.label_note("company", label_info, label_path.name))
    (out / f"org_dedup_report_{dc.today()}.txt").write_text(text, encoding="utf-8")
    log(json.dumps({k: summary[k] for k in ("stats", "category", "tiers", "label_file")}, indent=2))
    log(f"[dedup] DRY RUN — zero Pipedrive writes. Review CSV: {csv_path}")
    if args.post_task:
        files = ([label_path] if label_info["rows"] else []) + [csv_path]
        ok = dc.post_to_clickup(root, args.post_task, text, files)
        log(f"[dedup] ClickUp post {'ok' if ok else 'FAILED'} on task {args.post_task}")
        if ok and label_info["rows"]:
            dc.record_report(base, "orgs", csv_path, label_info["rows"])
    t = summary["tiers"]
    print(f"RESULT: orgs dry-run high={t.get('high', 0)} medium={t.get('medium', 0)} "
          f"review={t.get('review', 0)} name-only={t.get('name-only', 0)} csv={csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

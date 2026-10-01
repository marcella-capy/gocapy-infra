#!/usr/bin/env python3
"""pipedrive-person-dedup skill — whole-database duplicate-PERSON merge.

A name alone NEVER makes a duplicate (there are John Smiths everywhere). Every pair needs the
names to agree AND at least one second signal:

  second signals   same email (any address on the record; role inboxes like purchasing@ ignored)
                   same LinkedIn profile (/in/<slug>)
                   same organization
                   same direct phone (a number shared by 3+ different names is a switchboard: ignored)
                   same company email domain (free-mail domains ignored)

  names agree      exact   = same first + last after cleanup (accents, ", MBA", Jr/Sr/III/PhD/Dr)
                   fuzzy   = nickname pair (Bob/Robert, nicknames.json), initial ("J. Smith"),
                             abbreviated last name ("Lisa S." / "Lisa Sampson"), compound surname
                             ("Cesar Perez Morelos" / "Cesar Perez"), prefix (Chris/Christopher),
                             small typo (Jaro-Winkler >= 0.92)

Tiers (one per loser row):
  high     names agree (exact or fuzzy) AND same email or same LinkedIn profile - except same
           LinkedIn at a DIFFERENT org with no shared email (a job change) -> review
  medium   exact names AND same org, no conflicting LinkedIn
  review   fuzzy names AND (same org or phone); exact names + same direct phone at a different org;
           exact names + same email domain only; a loser tied
           to the survivor only through a third record; groups of 5+
  CONFLICT same email or LinkedIn but the names DISAGREE -> never merged; written to a separate
           bad-data CSV (almost always an enrichment tool attached the wrong profile/email)

Survivor: a full name first (never keep "Lisa S." over "Lisa Sampson" - names are fill-only, golden
rule 10, so we can't fix it after) -> most engagement (emails+activities+deals+notes) -> has Person
Research -> lowest id. Reads the daily snapshot (pd_cache) — zero Pipedrive calls on a dry run.

  python dedup_persons.py                                  # dry run -> shared-references/dedup/persons/<today>/
  python dedup_persons.py --post-task 86bc8kj30            # ...and post the report + CSVs to ClickUp as Kodie
                                                           # (incl. the sure-tier label import file)
  python dedup_persons.py --execute --plan <csv> --tiers high --limit 2500

--execute REQUIRES --plan and --tiers. Budget brake keeps Pipedrive's last 15%; a full backlog
takes several days — re-run the same command daily, merged losers are skipped via the ledger.
Name, phone and email are never written; joined text fields (job title, LinkedIn, Email
Validation, Research, City/State/Country) are restored to the survivor's pre-merge value.

Exit codes: 0 ok, 2 setup/plan problem, 3 merge failures during --execute.
"""
from __future__ import annotations

import argparse
import itertools
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
REFERENCES = SKILL / "references"
ORG_SKILL_SCRIPTS = SKILL.parent / "pipedrive-org-dedup" / "scripts"
sys.path.insert(0, str(ORG_SKILL_SCRIPTS))
import dedup_common as dc  # noqa: E402

log = dc.log

HONORIFIC = {"mr", "mrs", "ms", "miss", "dr", "prof", "sir", "jr", "sr", "ii", "iii", "iv", "v",
             "phd", "mba", "pe", "cpa", "pmp", "esq", "md", "cpm", "cscp", "cppm", "msc", "bsc"}
MAX_BLOCK = 400      # skip pairwise comparison inside a blocking key bigger than this
MAX_CLUSTER = 4      # clusters bigger than this go to review
SWITCHBOARD = 3      # a phone shared by this many different names is a main line


# -- names ----------------------------------------------------------------------------------

def load_nicknames() -> dict:
    data = json.loads((REFERENCES / "nicknames.json").read_text(encoding="utf-8"))
    idx = defaultdict(set)
    for gi, grp in enumerate(data.get("groups", [])):
        for n in grp:
            idx[n].add(gi)
    return idx


NICK = {}


class Name:
    __slots__ = ("first", "last", "surnames", "tokens")

    def __init__(self, raw: str):
        s = dc.ascii_fold(raw)
        s = re.sub(r"\(.*?\)|\[.*?\]", " ", s).split(",")[0]
        toks = [t for t in re.findall(r"[a-z]+", s.replace("'", "")) if t not in HONORIFIC]
        self.tokens = toks
        self.first = toks[0] if toks else ""
        self.last = toks[-1] if len(toks) > 1 else ""
        self.surnames = {t for t in toks[1:] if len(t) > 1}

    @property
    def full(self) -> bool:
        return len(self.last) > 1

    def key(self) -> str:
        return f"{self.first} {self.last}".strip()


def first_agree(a: str, b: str) -> int:
    """2 exact, 1 fuzzy, 0 no."""
    if not a or not b:
        return 0
    if a == b:
        return 2
    if len(a) == 1 or len(b) == 1:
        return 1 if a[0] == b[0] else 0
    if NICK.get(a, set()) & NICK.get(b, set()):
        return 1
    short, long_ = sorted((a, b), key=len)
    if len(short) >= 3 and long_.startswith(short):
        return 1
    return 1 if min(len(a), len(b)) >= 4 and dc.jaro_winkler(a, b) >= 0.92 else 0


def last_agree(a: Name, b: Name) -> int:
    if not a.last or not b.last:
        return 0
    if a.last == b.last:
        return 2
    if len(a.last) == 1 or len(b.last) == 1:          # "Lisa S." / "Lisa Sampson"
        return 1 if a.last[0] == b.last[0] else 0
    if a.surnames & b.surnames:                        # compound surnames
        return 1
    return 1 if min(len(a.last), len(b.last)) >= 4 and dc.jaro_winkler(a.last, b.last) >= 0.92 else 0


def names_agree(a: Name, b: Name) -> int:
    """2 exact, 1 fuzzy, 0 disagree. One-word names can only ever be fuzzy."""
    if not a.first or not b.first:
        return 0
    if not a.last or not b.last:
        return 1 if first_agree(a.first, b.first) else 0
    f, l = first_agree(a.first, b.first), last_agree(a, b)
    if f and l:
        return 2 if (f == 2 and l == 2) else 1
    if a.first == b.last and a.last == b.first:        # "Smith John"
        return 1
    return 0


# -- record access --------------------------------------------------------------------------

def as_int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def org_of(p):
    o = p.get("org_id")
    return (o.get("value") if isinstance(o, dict) else o) or None


def emails_of(p) -> list:
    out = []
    for e in p.get("email") or []:
        v = e.get("value") if isinstance(e, dict) else e
        if isinstance(v, str) and "@" in v:
            v = v.strip().lower()
            if v not in out:
                out.append(v)
    return out


def phones_of(p) -> set:
    out = set()
    for e in p.get("phone") or []:
        v = e.get("value") if isinstance(e, dict) else e
        d = re.sub(r"\D", "", str(v or ""))
        if len(d) >= 10:
            out.add(d[-10:])
    return out


def engagement(p) -> int:
    return sum(as_int(p.get(k)) for k in (
        "email_messages_count", "activities_count", "open_deals_count", "closed_deals_count",
        "notes_count", "participant_open_deals_count", "participant_closed_deals_count"))


# -- plan -----------------------------------------------------------------------------------

def build_plan(persons, li_key, research_key, free_domains, is_role_email):
    recs, names = {}, {}
    for p in persons:
        if p.get("id") is None or p.get("active_flag") is False:
            continue
        pid = int(p["id"])
        recs[pid] = p
        names[pid] = Name(p.get("name") or f"{p.get('first_name') or ''} {p.get('last_name') or ''}")

    email_of = {i: [e for e in emails_of(p) if not is_role_email(e)] for i, p in recs.items()}
    li_of = {i: dc.linkedin_slug(p.get(li_key), "in") for i, p in recs.items()}
    phone_of = {i: phones_of(p) for i, p in recs.items()}

    # Switchboards: a number carried by 3+ different people is a main line, not a direct phone.
    phone_names = defaultdict(set)
    for i, ph in phone_of.items():
        for x in ph:
            phone_names[x].add(names[i].key())
    switch = {x for x, ns in phone_names.items() if len(ns) >= SWITCHBOARD}

    blocks = {k: defaultdict(set) for k in ("email", "linkedin", "phone", "org", "domain")}
    for i, p in recs.items():
        n = names[i]
        init = n.last[:1] or n.first[:1]
        for e in email_of[i]:
            blocks["email"][e].add(i)
            dom = e.split("@", 1)[1]
            if dom not in free_domains and init:
                blocks["domain"][(dom, init)].add(i)
        if li_of[i]:
            blocks["linkedin"][li_of[i]].add(i)
        for x in phone_of[i] - switch:
            blocks["phone"][x].add(i)
        o = org_of(p)
        if o and init:
            blocks["org"][(o, init)].add(i)

    category = {}
    for k in ("email", "linkedin", "phone"):
        g = [s for s in blocks[k].values() if len(s) > 1]
        category[k] = {"groups": len(g), "extra_records": sum(len(s) - 1 for s in g)}

    pairs = set()
    skipped_blocks = 0
    for k, idx in blocks.items():
        for ids in idx.values():
            if len(ids) < 2:
                continue
            if len(ids) > MAX_BLOCK:
                skipped_blocks += 1
                continue
            for a, b in itertools.combinations(sorted(ids), 2):
                pairs.add((a, b))

    edges, conflicts = {}, []
    sig_counts = Counter()
    for a, b in pairs:
        pa, pb = recs[a], recs[b]
        agree = names_agree(names[a], names[b])
        shared_email = sorted(set(email_of[a]) & set(email_of[b]))
        shared_li = li_of[a] and li_of[a] == li_of[b]
        li_conflict = li_of[a] and li_of[b] and li_of[a] != li_of[b]
        same_org = org_of(pa) and org_of(pa) == org_of(pb)
        shared_phone = bool((phone_of[a] & phone_of[b]) - switch)
        doms_a = {e.split("@", 1)[1] for e in email_of[a]} - free_domains
        doms_b = {e.split("@", 1)[1] for e in email_of[b]} - free_domains
        same_dom = bool(doms_a & doms_b)
        sig = [s for s, on in (("email", shared_email), ("linkedin", shared_li), ("org", same_org),
                               ("phone", shared_phone), ("email-domain", same_dom)) if on]
        if shared_email or shared_li:
            if agree == 0:
                if names[a].full and names[b].full:
                    conflicts.append({
                        "shared": "email" if shared_email else "linkedin",
                        "value": shared_email[0] if shared_email else li_of[a],
                        "id_a": a, "name_a": pa.get("name"), "org_a": pa.get("org_name") or "",
                        "id_b": b, "name_b": pb.get("name"), "org_b": pb.get("org_name") or ""})
                continue
            # Same LinkedIn but a different org and no shared email is usually a job change: the old
            # record documents the old employer, so a human decides (Clay job-changer lane owns these).
            job_move = shared_li and not shared_email and not same_org and org_of(pa) and org_of(pb)
            tier = "review" if (li_conflict or job_move) else "high"
        elif li_conflict:
            continue
        elif same_org and agree == 2:
            tier = "medium"
        elif (same_org or shared_phone) and agree >= 1:
            tier = "review"
        elif same_dom and agree == 2:
            tier = "review"
        else:
            continue
        edges[(a, b)] = {"tier": tier, "sig": sig, "agree": agree}
        sig_counts[tier] += 1

    uf = dc.UnionFind()
    for a, b in edges:
        uf.union(a, b)
    rank = {"high": 0, "medium": 1, "review": 2}
    rows = []
    for members in uf.groups().values():
        if len(members) < 2:
            continue
        group = [recs[i] for i in members]
        s = sorted(group, key=lambda p: (not names[int(p["id"])].full, -engagement(p),
                                         not str(p.get(research_key) or "").strip(),
                                         -len(names[int(p["id"])].tokens), int(p["id"])))[0]
        sid = int(s["id"])
        disagree = any(names_agree(names[x], names[y]) == 0
                       for x, y in itertools.combinations(members, 2))
        for p in group:
            lid = int(p["id"])
            if lid == sid:
                continue
            e = edges.get((min(lid, sid), max(lid, sid)))
            notes = []
            if e:
                tier, sig, agree = e["tier"], e["sig"], e["agree"]
            else:
                tier, sig, agree = "review", [], names_agree(names[lid], names[sid])
                notes.append("linked only through another record")
            if len(members) > MAX_CLUSTER:
                tier = "review"
                notes.append(f"large group ({len(members)} records)")
            if disagree:
                tier = "review"
                notes.append("some names in this group disagree")
            if agree == 1:
                notes.append("names match loosely (nickname/initial/typo)")
            if e and "linkedin" in sig and "email" not in sig and "org" not in sig:
                notes.append("same LinkedIn at a different company - may be a job change")
            rows.append({
                "tier": tier, "match_on": "+".join(sig) or "-",
                "name_match": {2: "exact", 1: "fuzzy"}.get(agree, "no"),
                "survivor_id": sid, "survivor_name": s.get("name"),
                "survivor_org": s.get("org_name") or "", "survivor_email": (emails_of(s) or [""])[0],
                "survivor_reason": reason(s, names[sid], research_key),
                "survivor_added": (s.get("add_time") or "")[:10],
                "loser_id": lid, "loser_name": p.get("name"), "loser_org": p.get("org_name") or "",
                "loser_email": (emails_of(p) or [""])[0],
                "loser_added": (p.get("add_time") or "")[:10], "note": "; ".join(notes),
                "_rank": rank[tier]})
    rows.sort(key=lambda r: (r["_rank"], r["survivor_id"], r["loser_id"]))
    tiers = Counter(r["tier"] for r in rows)
    stats = {"persons_scanned": len(recs), "candidate_pairs": len(pairs),
             "switchboard_numbers_ignored": len(switch), "oversized_blocks_skipped": skipped_blocks,
             "duplicate_groups": len({r["survivor_id"] for r in rows}), "conflicts": len(conflicts)}
    return rows, conflicts, {"stats": stats, "category": category, "tiers": dict(tiers)}


def reason(p, n, research_key) -> str:
    bits = []
    if n.full:
        bits.append("full name")
    if engagement(p):
        bits.append(f"most engagement ({engagement(p)})")
    if str(p.get(research_key) or "").strip():
        bits.append("has Person Research")
    return ", ".join(bits) or "lowest id"


CSV_HEADER = ["tier", "match_on", "name_match", "survivor_id", "survivor_name", "survivor_org",
              "survivor_email", "survivor_reason", "survivor_added", "loser_id", "loser_name",
              "loser_org", "loser_email", "loser_added", "note"]
CONFLICT_HEADER = ["shared", "value", "id_a", "name_a", "org_a", "id_b", "name_b", "org_b"]


def summary_text(summary, csv_name, conflict_name, label_text) -> str:
    t, st = summary["tiers"], summary["stats"]
    return (
        f"Weekly people duplicate check - nothing has been merged yet.\n\n"
        f"I checked {st['persons_scanned']:,} people. {t.get('high', 0):,} records are copies I'm sure "
        f"about: the name matches AND they share an email or LinkedIn profile.\n\n"
        f"{label_text}\n\n"
        f"When you're done deleting, reply \"merge them\" here and I'll merge the rest of that sure list "
        f"into the record we keep, over the next few nights (Pipedrive limits changes per day). Copies "
        f"you already deleted are skipped. Names and phone numbers are never changed.\n\n"
        f"Left alone, not labelled: {t.get('medium', 0):,} likely and {t.get('review', 0):,} that need a "
        f"human look (full list: {csv_name}), plus {st['conflicts']:,} bad-data cases where two DIFFERENT "
        f"people share one email or LinkedIn ({conflict_name})."
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--plan", default=None)
    ap.add_argument("--tiers", default=None, help="high,medium,review or all")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--post-task", default=None)
    ap.add_argument("--capy-root", default=None)
    args = ap.parse_args()

    root = dc.find_capy_root(args.capy_root, SKILL)
    if root is None:
        log("ERROR: could not locate gocapy-claude-plugin/.../pd_cache.py. Pass --capy-root.")
        return 2
    dc.bootstrap(root)
    base = root / dc.DEDUP_OUT_REL / "persons"

    if args.execute:
        tiers = dc.parse_tiers(args.tiers)
        if not args.plan or not tiers:
            log("ERROR: --execute needs --plan <approved csv> AND --tiers <approved tiers>. Refusing.")
            return 2
        plan = Path(args.plan)
        if not plan.exists():
            log(f"ERROR: plan not found: {plan}")
            return 2
        from pipedrive_create import merge_persons, pd_call
        rows = dc.read_plan(plan, tiers)
        if not rows:
            log(f"ERROR: no rows in {plan.name} for tiers {sorted(tiers)}.")
            return 2
        out = dc.out_dir(root, "persons")
        log(f"[dedup] EXECUTING up to {args.limit or len(rows)} of {len(rows)} approved rows "
            f"({sorted(tiers)}) from {plan.name} — MERGE only, nothing deleted.")
        res = dc.execute_merges(rows, entity="persons", merge_fn=merge_persons, pd_call=pd_call,
                                out=out, ledger=base / "merged_ledger.jsonl", limit=args.limit)
        res.update({"plan": str(plan), "tiers": sorted(tiers)})
        rp = out / f"results_{dc.today()}.json"
        rp.write_text(json.dumps(res, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        log(f"[dedup] DONE — merged {res['merged_count']}, failed {res['failed_count']}, "
            f"skipped {res['skipped_count']}{' (stopped: ' + res['stopped'] + ')' if res['stopped'] else ''}.")
        print(f"RESULT: merged={res['merged_count']} failed={res['failed_count']} skipped={res['skipped_count']}")
        return 3 if res["failed"] else 0

    import pd_cache
    import pd_fields
    sys.path.insert(0, str(root / "gocapy-claude-plugin/go-capy-outreach/skills/hothawk-add-to-crm/scripts"))
    from snapshot_match import is_role_email
    NICK.update(load_nicknames())
    blocklist = json.loads((ORG_SKILL_SCRIPTS.parent / "references" / "dedup-domain-blocklist.json")
                           .read_text(encoding="utf-8"))
    free = {str(d).lower() for d in (blocklist.get("domains", blocklist) if isinstance(blocklist, dict)
                                     else blocklist) if not str(d).startswith("_")}
    persons = pd_cache.get_persons()
    rows, conflicts, summary = build_plan(persons, pd_fields.PERSON_LINKEDIN,
                                          pd_fields.PERSON_RESEARCH, free, is_role_email)
    out = dc.out_dir(root, "persons")
    csv_path = out / f"person_dedup_review_{dc.today()}.csv"
    conf_path = out / f"person_dedup_bad_data_{dc.today()}.csv"
    label_path = out / f"person_dedup_label_import_{dc.today()}.csv"
    dc.write_csv(csv_path, CSV_HEADER, rows)
    dc.write_csv(conf_path, CONFLICT_HEADER, conflicts)
    by_id = {int(p["id"]): p for p in persons if p.get("id") is not None}
    label_info = dc.write_label_csv(label_path, rows, by_id, pd_cache.get_field_maps()[0][0], "Person")
    summary["label_file"] = label_info
    (out / f"person_dedup_summary_{dc.today()}.json").write_text(
        json.dumps({"date": dc.today(), "snapshot": pd_cache.snapshot_date("persons"), **summary},
                   indent=2, ensure_ascii=False), encoding="utf-8")
    text = summary_text(summary, csv_path.name, conf_path.name,
                        dc.label_note("people", label_info, label_path.name))
    (out / f"person_dedup_report_{dc.today()}.txt").write_text(text, encoding="utf-8")
    log(json.dumps(summary, indent=2))
    log(f"[dedup] DRY RUN — zero Pipedrive writes. Review CSV: {csv_path}")
    if args.post_task:
        files = ([label_path] if label_info["rows"] else []) + [csv_path, conf_path]
        ok = dc.post_to_clickup(root, args.post_task, text, files)
        log(f"[dedup] ClickUp post {'ok' if ok else 'FAILED'} on task {args.post_task}")
        if ok and label_info["rows"]:
            dc.record_report(base, "persons", csv_path, label_info["rows"])
    t = summary["tiers"]
    print(f"RESULT: persons dry-run high={t.get('high', 0)} medium={t.get('medium', 0)} "
          f"review={t.get('review', 0)} conflicts={summary['stats']['conflicts']} csv={csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

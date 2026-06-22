#!/usr/bin/env python3
"""pipedrive-org-dedup skill — whole-database duplicate-organization merge.

Finds organizations that share the SAME normalized website domain and merges the duplicates
into one survivor. This is the standalone, monthly-maintenance version of the per-filter dedup
documented in org-research-agent-v2/SKILL.md Step 1c (which only runs inside a research sweep).

It REUSES the existing primitives in the gocapy-claude-plugin marketplace — no new merge or
Pipedrive API code lives here:
  - pd_cache.get_orgs()                  the single 429-safe daily-snapshot read of ALL orgs
  - pd_cache.get_field_maps()            to resolve the "Company Research" custom-field key
  - seed_resolver.normalize_domain()     strip scheme/www/path/port -> bare registrable domain
  - pipedrive_create.merge_orgs()        PUT /organizations/{loser}/merge {"merge_with_id": survivor}

Survivor pick (mirrors Step 1c): non-empty Company Research -> most linked people -> lowest id.
Only EXACT-domain groups merge; orgs with no/blank/blocklisted domain are skipped (never grouped).
Same-domain / different-name acquisitions DO merge (still an exact-domain match).

Two phases, HARD review gate — the skill never merges anything it derived on its own:
  python dedup_orgs.py                          # dry-run: writes a reviewable CSV + JSON, ZERO writes
  python dedup_orgs.py --execute --plan <csv>   # merge ONLY the rows in the human-approved CSV
  python dedup_orgs.py --execute --plan <csv> --limit 25   # cautious first batch from that CSV
  python dedup_orgs.py --out <path>             # base path for the dry-run CSV/JSON
  python dedup_orgs.py --capy-root <path>       # override sibling-repo autodetect

--execute REQUIRES --plan pointing at a dry-run CSV the user has reviewed. It re-derives nothing:
it merges exactly the loser->survivor pairs in that file. No CSV => no merges.

Exit codes: 0 ok, 2 sibling repo / primitives / plan not found, 3 merge failures during --execute.
"""
from __future__ import annotations

import argparse
import csv
import datetime
import json
import sys
from collections import defaultdict
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
REFERENCES = SKILL / "references"

# Relative locations of the reused primitives inside the gocapy-claude-plugin marketplace.
_PD_CACHE_REL = Path("gocapy-claude-plugin/go-capy-outreach/scripts")
_FINDPEOPLE_REL = Path(
    "gocapy-claude-plugin/go-capy-outreach/skills/find-people-at-organizations/scripts")


def log(*a):
    print(*a, file=sys.stderr)


def find_capy_root(explicit: "str | None") -> "Path | None":
    """Locate the dir that holds gocapy-claude-plugin (the marketplaces root). Walk up from this
    skill, then fall back to a glob — same approach as domain-inventory's client-domains lookup."""
    if explicit:
        p = Path(explicit)
        return p if (p / _PD_CACHE_REL / "pd_cache.py").exists() else None
    for base in [SKILL, *SKILL.parents]:
        if (base / _PD_CACHE_REL / "pd_cache.py").exists():
            return base
    for base in SKILL.parents:
        for cand in base.glob("**/gocapy-claude-plugin/go-capy-outreach/scripts/pd_cache.py"):
            return cand.parents[2].parent  # .../marketplaces
    return None


def load_blocklist() -> set[str]:
    """Normalized domains that must NEVER group (parked pages, shared ESP/registrar hosts,
    generic mailbox providers). Unparseable/comment keys (leading '_') are ignored."""
    path = REFERENCES / "dedup-domain-blocklist.json"
    if not path.exists():
        log(f"WARN: blocklist not found at {path} — proceeding with empty blocklist")
        return set()
    data = json.loads(path.read_text(encoding="utf-8"))
    domains = data.get("domains", data) if isinstance(data, dict) else data
    return {str(d).strip().lower() for d in domains if d and not str(d).startswith("_")}


def load_org_exclusions() -> list[str]:
    """Org-name substrings (lowercased) that must NEVER be merged — any domain group containing
    one is skipped entirely and left for a human (e.g. large primes with intentional division
    records). Comment keys (leading '_') are ignored."""
    path = REFERENCES / "dedup-org-exclusions.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    names = data.get("names", data) if isinstance(data, dict) else data
    return [str(n).strip().lower() for n in names if n and not str(n).startswith("_")]


def is_excluded(org: dict, exclusions: list[str]) -> bool:
    name = (org.get("name") or "").lower()
    return any(ex in name for ex in exclusions)


def has_research(org: dict, research_key: "str | None") -> bool:
    if not research_key:
        return False
    return bool(str(org.get(research_key) or "").strip())


def people_count(org: dict) -> int:
    try:
        return int(org.get("people_count") or 0)
    except (TypeError, ValueError):
        return 0


def pick_survivor(group: list[dict], research_key: "str | None") -> dict:
    """Non-empty Company Research -> most linked people -> lowest id (deterministic)."""
    return sorted(
        group,
        key=lambda o: (not has_research(o, research_key), -people_count(o), int(o["id"])),
    )[0]


def build_plan(orgs: dict, normalize, research_key, blocklist, exclusions):
    """Group orgs by exact normalized domain; return (merges, stats).

    merges: [{"domain","survivor":{...},"losers":[{...}]}]   stats: counts for the report.
    """
    by_domain: dict[str, list[dict]] = defaultdict(list)
    no_domain = blocklisted = 0
    for org in orgs.values():
        domain = normalize(org.get("website") or "")
        if not domain:
            no_domain += 1
            continue
        if domain in blocklist:
            blocklisted += 1
            continue
        by_domain[domain].append(org)

    merges = []
    excluded = 0
    for domain, group in by_domain.items():
        if len(group) < 2:
            continue
        if any(is_excluded(o, exclusions) for o in group):
            excluded += 1
            log(f"[dup-excluded] {domain} — protected org name, skipped (left for human)")
            continue
        survivor = pick_survivor(group, research_key)
        losers = [o for o in group if int(o["id"]) != int(survivor["id"])]
        if not losers:
            continue
        reason = ("has Company Research" if has_research(survivor, research_key)
                  else f"most people ({people_count(survivor)})" if people_count(survivor)
                  else "lowest id")
        merges.append({
            "domain": domain,
            "survivor": {"id": int(survivor["id"]), "name": survivor.get("name"),
                         "people": people_count(survivor), "research": has_research(survivor, research_key),
                         "reason": reason},
            "losers": [{"id": int(o["id"]), "name": o.get("name"), "people": people_count(o)}
                       for o in losers],
        })

    merges.sort(key=lambda m: m["domain"])
    stats = {
        "orgs_scanned": len(orgs),
        "duplicate_domains": len(merges),
        "planned_merges": sum(len(m["losers"]) for m in merges),
        "no_domain_skipped": no_domain,
        "blocklist_skipped": blocklisted,
        "excluded_groups_skipped": excluded,
    }
    return merges, stats


def print_report(merges, stats):
    for m in merges:
        s = m["survivor"]
        log(f"\n  {m['domain']}  (keep #{s['id']} {s['name']!r} — {s['reason']})")
        for loser in m["losers"]:
            log(f"      merge #{loser['id']} {loser['name']!r}  ({loser['people']} ppl)  -> #{s['id']}")
    log("\n" + "=" * 70)
    log(f"  orgs scanned ............ {stats['orgs_scanned']}")
    log(f"  duplicate domains ....... {stats['duplicate_domains']}")
    log(f"  planned merges .......... {stats['planned_merges']}")
    log(f"  skipped (no domain) ..... {stats['no_domain_skipped']}")
    log(f"  skipped (blocklist) ..... {stats['blocklist_skipped']}")
    log(f"  skipped (excluded orgs) . {stats.get('excluded_groups_skipped', 0)}")
    log("=" * 70)


CSV_HEADER = ["domain", "survivor_id", "survivor_name", "survivor_reason",
              "loser_id", "loser_name", "loser_people"]


def write_plan_csv(merges, path: Path) -> None:
    """One row per planned merge (loser -> survivor). This is the artifact the user reviews AND
    the exact file --execute consumes — what they approve is what runs."""
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_HEADER)
        for m in merges:
            s = m["survivor"]
            for loser in m["losers"]:
                w.writerow([m["domain"], s["id"], s["name"], s["reason"],
                            loser["id"], loser["name"], loser["people"]])


def read_plan_csv(path: Path) -> list[dict]:
    """Read approved loser->survivor pairs back from a reviewed dry-run CSV. --execute acts ONLY
    on these rows; it never re-derives the plan."""
    rows = []
    with path.open(newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            try:
                rows.append({"domain": r.get("domain", ""),
                             "survivor": int(r["survivor_id"]), "loser": int(r["loser_id"]),
                             "loser_name": r.get("loser_name", "")})
            except (KeyError, ValueError):
                continue  # skip malformed / commented rows
    return rows


def execute_merges(pairs, merge_orgs, limit: "int | None"):
    """Merge each approved loser->survivor pair via the Pipedrive MERGE endpoint (never delete).
    Returns (merged, failed) lists."""
    merged, failed = [], []
    for i, p in enumerate(pairs):
        if limit is not None and i >= limit:
            break
        loser_id, survivor_id, domain = p["loser"], p["survivor"], p["domain"]
        try:
            status, resp = merge_orgs(loser_id, survivor_id, dry_run=False)
        except Exception as exc:  # noqa: BLE001 — keep the batch going, record the failure
            status, resp = "error", {"exception": str(exc)}
        if status == "merged":
            log(f"[org-merge] {loser_id} -> {survivor_id} ({domain})")
            merged.append({"loser": loser_id, "survivor": survivor_id, "domain": domain})
        else:
            log(f"[org-merge-FAILED] {loser_id} -> {survivor_id} ({domain}): {resp}")
            failed.append({"loser": loser_id, "survivor": survivor_id,
                           "domain": domain, "response": resp})
    return merged, failed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true",
                    help="Run merges. REQUIRES --plan (a reviewed dry-run CSV). Default is dry-run.")
    ap.add_argument("--plan", default=None,
                    help="Path to the human-approved dry-run CSV. Required (and only used) with --execute.")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap the number of merges performed (cautious first batch).")
    ap.add_argument("--out", default=None, help="Base path for the dry-run CSV/JSON or results JSON.")
    ap.add_argument("--capy-root", default=None,
                    help="Override autodetect of the marketplaces dir holding gocapy-claude-plugin.")
    args = ap.parse_args()

    root = find_capy_root(args.capy_root)
    if root is None:
        log("ERROR: could not locate gocapy-claude-plugin/.../pd_cache.py. Pass --capy-root.")
        return 2
    sys.path.insert(0, str(root / _PD_CACHE_REL))
    sys.path.insert(0, str(root / _FINDPEOPLE_REL))
    log(f"[dedup] capy-root: {root}")
    today = datetime.date.today().strftime("%Y%m%d")

    # ── EXECUTE: merge ONLY the human-approved CSV. Re-derives nothing; never deletes. ──────────
    if args.execute:
        if not args.plan:
            log("ERROR: --execute requires --plan <reviewed dry-run CSV>. Refusing to merge "
                "anything that wasn't reviewed. Run a dry run first, review the CSV, then pass it.")
            return 2
        plan_path = Path(args.plan)
        if not plan_path.exists():
            log(f"ERROR: --plan file not found: {plan_path}")
            return 2
        try:
            from pipedrive_create import merge_orgs
        except ImportError as exc:
            log(f"ERROR: failed to import merge primitive from {root}: {exc}")
            return 2
        pairs = read_plan_csv(plan_path)
        if not pairs:
            log(f"ERROR: no valid merge rows in {plan_path}.")
            return 2
        n = min(args.limit, len(pairs)) if args.limit else len(pairs)
        log(f"\n[dedup] EXECUTING {n} of {len(pairs)} approved merges from {plan_path.name} "
            f"(Pipedrive MERGE — loser folds into survivor, no record is deleted)...")
        merged, failed = execute_merges(pairs, merge_orgs, args.limit)
        out_path = Path(args.out) if args.out else Path(f"dedup_orgs_results_{today}.json")
        out_path.write_text(json.dumps(
            {"date": today, "mode": "execute", "plan": str(plan_path),
             "approved_pairs": len(pairs), "merged_count": len(merged), "failed_count": len(failed),
             "merged": merged, "failed": failed}, indent=2, ensure_ascii=False), encoding="utf-8")
        log(f"\n[dedup] DONE — merged {len(merged)}, failed {len(failed)}. Results -> {out_path}")
        return 3 if failed else 0

    # ── DRY RUN (default): build plan, print, write the reviewable CSV (+ JSON). ZERO writes. ───
    try:
        import pd_cache
        from seed_resolver import normalize_domain
    except ImportError as exc:
        log(f"ERROR: failed to import reused primitives from {root}: {exc}")
        return 2
    orgs = pd_cache.get_orgs()
    (_, _), (_org_by_key, org_by_name) = pd_cache.get_field_maps()
    research_key = (org_by_name.get("Company Research") or {}).get("key")
    blocklist = load_blocklist()
    exclusions = load_org_exclusions()
    if exclusions:
        log(f"[dedup] protected org names (never merged): {', '.join(exclusions)}")

    merges, stats = build_plan(orgs, normalize_domain, research_key, blocklist, exclusions)
    print_report(merges, stats)

    base = Path(args.out) if args.out else Path(f"dedup_orgs_dryrun_{today}")
    base = base.with_suffix("")  # we add our own extensions
    csv_path, json_path = base.with_suffix(".csv"), base.with_suffix(".json")
    write_plan_csv(merges, csv_path)
    json_path.write_text(json.dumps({"date": today, "mode": "dry-run", "stats": stats,
                                     "merges": merges}, indent=2, ensure_ascii=False),
                         encoding="utf-8")
    log(f"\n[dedup] DRY RUN — zero writes to Pipedrive.")
    log(f"[dedup] Review CSV: {csv_path}")
    log(f"[dedup] Then, after approval:  python dedup_orgs.py --execute --plan {csv_path.name} [--limit N]")
    return 0
    return 3 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

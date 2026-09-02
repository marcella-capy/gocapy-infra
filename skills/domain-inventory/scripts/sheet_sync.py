#!/usr/bin/env python3
"""Sync the '3.0 Renewal' tab of the Cold Outreach Domains sheet from live sources.

Fills, per domain: Principal, Type (registrar), Registered On, Expires on, Auto Renewal.
Appends any Porkbun domain the sheet is missing.

Reuses pull_porkbun()/load_env() from domain_inventory.py - one Porkbun client.

COLUMNS ARE RESOLVED BY HEADER NAME, never by position. The tab's layout has already
changed once mid-project (headers moved from row 2 to row 1 and two columns were
inserted), and positional access silently wrote into the wrong column. If a header is
renamed, this script reports the columns it could not find and writes nothing.

SAFETY
  - dry-run by default; --apply is required to write
  - snapshots the tab to references/sheet-backups/ before any write
  - writes only the columns it owns, only on the '3.0 Renewal' tab
  - NEVER overwrites a human annotation in Auto Renewal ('cancelled', 'XX', free text);
    conflicts are reported, not resolved
  - domains Porkbun does not hold (SiteGround / External) keep their existing expiry:
    there is no live source for them, so a guess is worse than the current value

    py sheet_sync.py            # report what would change
    py sheet_sync.py --apply    # write it
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
sys.path.insert(0, str(HERE))

from domain_inventory import load_env, pull_porkbun  # noqa: E402

SHEET_ID = "178xVEdckU_4Kkh65oRTHsy8wL-Z6ZU4BBZ037Cv0teg"
TAB = "3.0 Renewal"
BACKUP_DIR = SKILL / "references" / "sheet-backups"
STATE = SKILL / "references" / "renewal-last-sync.json"

# Auto Renewal values this script owns. Anything else is a human note and is left alone.
MANAGED = {"", "ON", "OFF"}

# header text (lowercased, trimmed) -> internal field
WANT = {
    "principal": "principal",
    "operating company": "opco", "operating co": "opco", "opco": "opco",
    "domains": "domain", "domain": "domain",
    "type": "type",
    "registered on": "registered", "registered": "registered",
    "expires on": "expires", "expiry": "expires", "expiration": "expires",
    "auto renewal": "renewal", "auto-renew": "renewal", "auto renew": "renewal",
}


def log(*a):
    print(*a, file=sys.stderr)


def norm(d: str) -> str:
    return d.strip().strip(".").lower()


def fmt_date(iso: str) -> str:
    """2027-07-22 -> 'Jul 22, 2027', matching the tab's existing style."""
    if not iso:
        return ""
    try:
        d = datetime.date.fromisoformat(iso[:10])
    except ValueError:
        return iso
    return f"{d.strftime('%b')} {d.day}, {d.year}"


def find_plugin_repo() -> Path | None:
    """Locate the sibling gocapy-claude-plugin checkout (holds the principal map)."""
    rel = Path("gocapy-claude-plugin/go-capy-outreach/skills/domain-rotation/references")
    for base in [SKILL, *SKILL.parents]:
        cand = base / rel
        if cand.is_dir():
            return cand
    return None


def load_principal_map() -> tuple[dict, dict]:
    """(domain -> principal, alias -> canonical). Owned by the domain-rotation skill so
    the rotation tab and the renewal tab can never disagree about who owns a domain."""
    ref = find_plugin_repo()
    if not ref:
        return {}, {}
    pm, al = {}, {}
    f = ref / "domain-principals.json"
    if f.is_file():
        pm = {k.lower(): v for k, v in json.loads(f.read_text(encoding="utf-8")).items()}
    f = ref / "rotation-exclusions.json"
    if f.is_file():
        al = {k.lower(): v for k, v in
              json.loads(f.read_text(encoding="utf-8")).get("principal_aliases", {}).items()
              if not k.startswith("_")}
    return pm, al


def load_opco_map() -> dict:
    """domain -> operating company, for the workspaces that hold more than one business.

    Only HV OpCos does today (Harvey Vogel / Seconn / Workplace Systems NH). Lives beside
    the principal map in the domain-rotation skill so the renewal tab, the rotation tab and
    the turn-on tooling all read the same file - the split existed only in someone's head
    until 2026-08-28, and a domain went into the wrong client's campaign list because of it.
    """
    ref = find_plugin_repo()
    if not ref:
        return {}
    f = ref / "hv-opco-map.json"
    if not f.is_file():
        return {}
    out = {}
    doc = json.loads(f.read_text(encoding="utf-8"))
    for opco, info in (doc.get("operating_companies") or {}).items():
        for d in info.get("domains", []):
            out[d.lower()] = opco
    return out


def hothawk_workspace_of() -> dict:
    """domain -> HotHawk workspace name, the fallback when the map has no entry."""
    ref = find_plugin_repo()
    if not ref:
        return {}
    dh_dir = ref.parents[1] / "domain-health" / "scripts"
    if not dh_dir.is_dir():
        return {}
    sys.path.insert(0, str(dh_dir))
    try:
        import dh_common as dh
        out = {}
        for ws in dh.hh_workspaces():
            for m in dh.hh_mailboxes(ws["id"]):
                out.setdefault((m.get("domain") or "").lower(), ws["name"])
        return out
    except Exception as e:  # never let an optional enrichment break the sync
        log(f"[warn] HotHawk lookup unavailable: {type(e).__name__}: {e}")
        return {}


def open_tab():
    from google.oauth2.service_account import Credentials
    import gspread
    key = load_env().get("GOOGLE_SHEETS_SA_KEY")
    if not key or not Path(key).is_file():
        sys.exit("GOOGLE_SHEETS_SA_KEY missing or not a file in ~/.claude/global.env")
    gc = gspread.authorize(Credentials.from_service_account_file(
        key, scopes=["https://www.googleapis.com/auth/spreadsheets"]))
    return gc.open_by_key(SHEET_ID).worksheet(TAB)


def resolve_columns(grid: list) -> tuple[int, dict]:
    """(header_row_index, {field: col_index}) by NAME. Exits if a required one is absent."""
    for i, row in enumerate(grid[:8]):
        cells = [c.strip().lower() for c in row]
        if not any(c in ("domains", "domain") for c in cells):
            continue
        cols = {}
        for j, c in enumerate(cells):
            f = WANT.get(c)
            if f and f not in cols:
                cols[f] = j
        if "domain" in cols:
            return i, cols
    sys.exit(f"could not find a header row on '{TAB}' - looked for a 'Domains' column "
             f"in the first 8 rows")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--price-fallback", type=float, default=11.06)
    a = ap.parse_args()

    env = load_env()
    pb_rows, err = pull_porkbun(env, a.price_fallback)
    if err:
        sys.exit(f"Porkbun pull failed: {err}")
    pb = {norm(r["domain"]): r for r in pb_rows}
    log(f"Porkbun: {len(pb)} domains")

    pmap, alias = load_principal_map()
    opco = load_opco_map()
    ws_of = hothawk_workspace_of()
    log(f"principal map: {len(pmap)} | HotHawk domains: {len(ws_of)}")

    def principal_for(d: str) -> str:
        raw = pmap.get(d) or ws_of.get(d) or ""
        return alias.get(raw.strip().lower(), raw.strip())

    ws = open_tab()
    grid = ws.get_all_values()
    hdr_i, cols = resolve_columns(grid)
    missing_cols = [f for f in ("principal", "type", "registered", "expires", "renewal")
                    if f not in cols]
    log(f"'{TAB}': header on row {hdr_i+1}, columns {cols}")
    if missing_cols:
        log(f"[warn] no column for: {missing_cols} - those fields will not be written")

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    (BACKUP_DIR / f"renewal-{stamp}.json").write_text(json.dumps(grid, indent=1),
                                                      encoding="utf-8")
    log(f"backup -> references/sheet-backups/renewal-{stamp}.json")

    width = max(len(r) for r in grid) if grid else 0
    width = max(width, max(cols.values()) + 1)
    rows = [list(r) + [""] * (width - len(r)) for r in grid[hdr_i + 1:]]

    seen, updates, conflicts = set(), [], []
    C = cols

    def put(row, field, value):
        """Write a field only if we own that column and the value actually changes."""
        if field not in C or value in (None, ""):
            return False
        if row[C[field]].strip() == str(value).strip():
            return False
        row[C[field]] = value
        return True

    for r in rows:
        dom = norm(r[C["domain"]])
        if not dom:
            continue
        seen.add(dom)
        before = list(r)
        r[C["domain"]] = r[C["domain"]].strip()      # strip stray trailing newlines

        put(r, "principal", principal_for(dom))
        put(r, "opco", opco.get(dom, ""))

        p = pb.get(dom)
        if p:
            put(r, "type", "Porkbun")
            put(r, "registered", fmt_date(p.get("registered", "")))
            put(r, "expires", fmt_date(p["expiration"]))
            note = r[C["renewal"]].strip() if "renewal" in C else ""
            if note.upper() in MANAGED:
                put(r, "renewal", p["renewal"])
            elif (note.lower() == "cancelled") != (p["renewal"] == "OFF"):
                conflicts.append((dom, note, p["renewal"]))
        if r != before:
            updates.append((dom, before, list(r)))

    missing = sorted(d for d in pb if d not in seen)
    for d in missing:
        p = pb[d]
        row = [""] * width
        row[C["domain"]] = p["domain"]
        put(row, "principal", principal_for(d))
        put(row, "opco", opco.get(d, ""))
        put(row, "type", "Porkbun")
        put(row, "registered", fmt_date(p.get("registered", "")))
        put(row, "expires", fmt_date(p["expiration"]))
        put(row, "renewal", p["renewal"])
        rows.append(row)

    filled_p = sum(1 for r in rows if "principal" in C and r[C["principal"]].strip())
    filled_r = sum(1 for r in rows if "registered" in C and r[C["registered"]].strip())
    filled_o = sum(1 for r in rows if "opco" in C and r[C["opco"]].strip())

    print(f"\n=== '{TAB}' sync {'(APPLY)' if a.apply else '(DRY RUN)'} ===")
    print(f"rows in tab      : {len(seen)}")
    print(f"rows to change   : {len(updates)}")
    print(f"rows to append   : {len(missing)}")
    print(f"conflicts        : {len(conflicts)}")
    print(f"principal filled : {filled_p}/{len(rows)}")
    print(f"registered filled: {filled_r}/{len(rows)}")
    if "opco" in C:
        print(f"opco filled      : {filled_o}/{len(opco)} known multi-business domains")
    elif opco:
        print(f"[warn] no 'Operating Company' column on the tab - "
              f"{len(opco)} domain(s) have one and it will not be written")

    if updates:
        print(f"\n-- sample changes ({min(12, len(updates))} of {len(updates)}) --")
        inv = {v: k for k, v in C.items()}
        for dom, b, af in updates[:12]:
            ch = [f"{inv.get(j, j)}: {b[j]!r}->{af[j]!r}"
                  for j in range(len(af)) if b[j] != af[j]]
            print(f"  {dom:<30} {'; '.join(ch)[:110]}")
    if missing:
        print(f"\n-- append ({len(missing)}) --")
        for d in missing[:15]:
            print(f"  {pb[d]['domain']}")
    if conflicts:
        print("\n-- CONFLICTS (left untouched, decide by hand) --")
        for dom, note, live in conflicts:
            print(f"  {dom:<30} sheet says {note!r}, Porkbun auto-renew is {live}")

    no_principal = [r[C["domain"]] for r in rows
                    if "principal" in C and not r[C["principal"]].strip()]
    if no_principal:
        print(f"\n-- no principal found ({len(no_principal)}) --")
        print("  " + ", ".join(no_principal[:12])
              + (f" ... +{len(no_principal)-12}" if len(no_principal) > 12 else ""))

    if not a.apply:
        print("\nDry run. Re-run with --apply to write.")
        return 0

    last = max(C.values())
    end_col = chr(ord("A") + last) if last < 26 else chr(ord("A") + last // 26 - 1) + chr(ord("A") + last % 26)
    body = [r[:last + 1] for r in rows]
    ws.update(range_name=f"A{hdr_i+2}:{end_col}{hdr_i+1+len(body)}",
              values=body, value_input_option="USER_ENTERED")
    print(f"\nWROTE A{hdr_i+2}:{end_col}{hdr_i+1+len(body)} ({len(body)} rows)")

    STATE.write_text(json.dumps({
        "synced_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "porkbun_count": len(pb), "changed": len(updates),
        "appended": [pb[d]["domain"] for d in missing],
        "conflicts": conflicts, "no_principal": no_principal,
    }, indent=1), encoding="utf-8")
    print(f"RESULT: renewal tab synced - {len(updates)} changed, {len(missing)} appended")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

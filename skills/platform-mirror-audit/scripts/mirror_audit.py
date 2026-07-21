#!/usr/bin/env python3
"""Audit HotHawk <-> PlusVibe mailbox parity and emit ready-to-upload account JSON.

The invariant (per mailbox-onboarder SKILL): **PlusVibe subset of HotHawk** — every
PlusVibe sending mailbox must also exist on HotHawk; HotHawk may hold extra warmup
mailboxes that are intentionally NOT on PlusVibe.

So gaps come in two flavours:
  1. PV-not-on-HH  -> HARD violation of the invariant. Must be added to HotHawk.
  2. HH-not-on-PV  -> usually fine (warmup extras). Split into:
       - whole domains absent from PV entirely  -> likely a real sender never mirrored
       - partial extras on already-mirrored domains -> almost always warmup, leave alone

Reads the domain-health inventory (source of truth for who is live where), pulls
passwords/hosts from the consolidated onboarding credential CSVs, and writes:
  - out/hh_<workspace>.json         one file per HotHawk workspace that needs adds
  - out/pv_<workspace>.json         one file per PlusVibe workspace that needs adds
  - out/report.md                   human summary (gaps, no-password, unassigned)

It NEVER uploads. It only tells you what to upload.

Usage:
  py mirror_audit.py                 # rebuild inventory + audit + write out/
  py mirror_audit.py --no-rebuild    # use the existing inventory.json as-is
  py mirror_audit.py --print         # also dump report.md to stdout
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL_DIR = HERE.parent
CONFIG_PATH = SKILL_DIR / "references" / "config.json"
OUT_DIR = SKILL_DIR / "out"

# Cross-repo: reuse the domain-health plumbing (live workspace ids, inventory build).
MARKETPLACES = HERE.parents[3]          # .../marketplaces
DH_SCRIPTS = (MARKETPLACES / "gocapy-claude-plugin" / "go-capy-outreach" /
              "skills" / "domain-health" / "scripts")
sys.path.insert(0, str(DH_SCRIPTS))
import dh_common as dh  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

CRED_FIELDS = ("first_name", "last_name", "daily_limit", "smtp_host", "smtp_port",
               "imap_host", "imap_port", "tags", "warmup_custom_words",
               "warmup_max_daily_limit", "warmup_daily_limit")


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


# ── credential index ─────────────────────────────────────────────────────────

def build_cred_index(dirs: list[str]) -> dict[str, dict]:
    """email(lower) -> {password, host/port, names, tag, warmup...}. Newest file wins."""
    files: list[Path] = []
    for d in dirs:
        base = Path(d).expanduser()
        if base.exists():
            files.extend(base.rglob("*.csv"))
    files.sort(key=lambda p: p.stat().st_mtime)  # oldest first -> newest overwrites
    index: dict[str, dict] = {}
    for f in files:
        try:
            with f.open(newline="", encoding="utf-8-sig") as fh:
                rd = csv.DictReader(fh)
                if not rd.fieldnames or "email" not in rd.fieldnames or "password" not in rd.fieldnames:
                    continue
                for row in rd:
                    email = (row.get("email") or "").strip().lower()
                    pw = (row.get("password") or "").strip()
                    if not email or not pw:
                        continue
                    rec = {"email": email, "password": pw, "_source": f.name}
                    for k in CRED_FIELDS:
                        if row.get(k) not in (None, ""):
                            rec[k] = row[k].strip()
                    index[email] = rec
        except (OSError, csv.Error):
            continue
    return index


def cred_for(email: str, idx: dict[str, dict]) -> dict:
    """Credential row for an email. Host is ALWAYS forced to the SiteGround canonical
    `mail.<address-domain>` (993/465) — never the CSV's host, which sometimes carries a
    sibling domain and fails the onboarder's domain-match gate (auth rejected)."""
    domain = email.split("@", 1)[1]
    rec = dict(idx.get(email, {}))
    rec["email"] = email
    host = f"mail.{domain}"
    rec["imap_host"] = host
    rec["smtp_host"] = host
    rec["imap_port"] = "993"
    rec["smtp_port"] = "465"
    return rec


# ── payload builders ─────────────────────────────────────────────────────────

def hh_payload(rec: dict, workspace_id: str) -> dict:
    email, pw = rec["email"], rec.get("password")
    return {
        "workspaceId": workspace_id,
        "email": email,
        "imapHost": rec["imap_host"], "imapPort": int(rec["imap_port"]),
        "smtpHost": rec["smtp_host"], "smtpPort": int(rec["smtp_port"]),
        "imapUsername": email, "imapPassword": pw,
        "smtpUsername": email, "smtpPassword": pw,
    }


def pv_payload(rec: dict) -> dict:
    """PlusVibe bulk-account fields (matches the consolidated-CSV upload schema)."""
    email, pw = rec["email"], rec.get("password")
    return {
        "first_name": rec.get("first_name", ""),
        "last_name": rec.get("last_name", ""),
        "email": email,
        "username": email, "password": pw,
        "smtp_username": email, "smtp_password": pw,
        "smtp_host": rec["smtp_host"], "smtp_port": int(rec["smtp_port"]),
        "imap_host": rec["imap_host"], "imap_port": int(rec["imap_port"]),
        "daily_limit": int(rec.get("daily_limit", 10)),
        "tags": rec.get("tags", ""),
        "warmup_custom_words": rec.get("warmup_custom_words", ""),
    }


# ── audit ────────────────────────────────────────────────────────────────────

def audit(cfg: dict, inv: dict, cred: dict[str, dict]) -> dict:
    exclude = {d.lower() for d in cfg["exclude_domains"]}
    pv_ws_ids = cfg["pv_workspaces"]
    tag_map = {k.upper(): v for k, v in cfg["client_tag_to_pv_workspace"].items()}

    hh_ws_id = {w["name"]: w["id"] for w in dh.hh_workspaces()}

    hh_adds: dict[str, list] = {}     # workspace name -> [payload]
    pv_adds: dict[str, list] = {}     # workspace label -> [payload]
    no_password: list[dict] = []      # gap emails with no credential found
    unassigned: list[dict] = []       # can't resolve a target workspace
    warmup_extras: list[dict] = []    # HH-not-on-PV partials (informational)

    domains = inv["domains"]
    for dom in sorted(domains):
        if dom in exclude:
            continue
        mbs = domains[dom]["mailboxes"]
        hh = {m["email"] for m in mbs if m["platform"] == "hothawk"}
        pv = {m["email"] for m in mbs if m["platform"] == "plusvibe"}
        hh_ws = sorted({m["workspace"] for m in mbs if m["platform"] == "hothawk"})
        pv_ws = sorted({m["workspace"] for m in mbs if m["platform"] == "plusvibe"})

        # (1) PV-not-on-HH -> invariant violation -> add to HotHawk
        for email in sorted(pv - hh):
            rec = cred_for(email, cred)
            target_ws = hh_ws[0] if hh_ws else None
            entry = {"email": email, "domain": dom, "reason": "pv-not-on-hh",
                     "target": target_ws, "has_password": bool(rec.get("password"))}
            if not rec.get("password"):
                no_password.append(entry); continue
            if not target_ws or target_ws not in hh_ws_id:
                unassigned.append(entry); continue
            hh_adds.setdefault(target_ws, []).append(hh_payload(rec, hh_ws_id[target_ws]))

        # (2) HH-not-on-PV
        miss_pv = sorted(hh - pv)
        whole_domain_absent = not pv     # domain has NO PlusVibe presence at all
        for email in miss_pv:
            rec = cred_for(email, cred)
            # resolve target PV workspace label
            if pv_ws:
                label = pv_ws[0]
            else:
                tag = (rec.get("tags") or "").upper().strip()
                label = tag_map.get(tag)
            entry = {"email": email, "domain": dom,
                     "reason": "hh-not-on-pv-wholedomain" if whole_domain_absent
                               else "hh-not-on-pv-partial",
                     "target": label, "has_password": bool(rec.get("password"))}
            if not whole_domain_absent:
                # partial extra on an already-mirrored domain -> almost always warmup
                warmup_extras.append(entry)
                continue
            if not rec.get("password"):
                no_password.append(entry); continue
            if not label or label not in pv_ws_ids:
                unassigned.append(entry); continue
            pv_adds.setdefault(label, []).append(pv_payload(rec))

    return {"hh_adds": hh_adds, "pv_adds": pv_adds, "no_password": no_password,
            "unassigned": unassigned, "warmup_extras": warmup_extras,
            "hh_ws_id": hh_ws_id, "pv_ws_ids": pv_ws_ids}


# ── report ───────────────────────────────────────────────────────────────────

def write_outputs(res: dict) -> str:
    OUT_DIR.mkdir(exist_ok=True)
    written = []
    for ws, rows in sorted(res["hh_adds"].items()):
        p = OUT_DIR / f"hh_{slug(ws)}.json"
        p.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        written.append((p.name, len(rows)))
    for label, rows in sorted(res["pv_adds"].items()):
        p = OUT_DIR / f"pv_{slug(label)}.json"
        p.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        written.append((p.name, len(rows)))

    L = ["# Platform mirror audit", "",
         "Invariant: **PlusVibe ⊆ HotHawk**. Files written to `out/` are upload-ready; nothing was uploaded.", ""]

    L.append("## 1. MUST FIX — on PlusVibe but missing from HotHawk (invariant violation)")
    if res["hh_adds"]:
        for ws, rows in sorted(res["hh_adds"].items()):
            L.append(f"### HotHawk / {ws}  ({len(rows)}) → `out/hh_{slug(ws)}.json`")
            L += [f"- {r['email']}" for r in rows]
    else:
        L.append("_none_")
    L.append("")

    L.append("## 2. CANDIDATES — whole domains live on HotHawk but absent from PlusVibe")
    if res["pv_adds"]:
        for label, rows in sorted(res["pv_adds"].items()):
            L.append(f"### PlusVibe / {label}  ({len(rows)}) → `out/pv_{slug(label)}.json`")
            L += [f"- {r['email']}" for r in rows]
    else:
        L.append("_none_")
    L.append("")

    L.append("## 3. IGNORE — HotHawk warmup extras on already-mirrored domains (expected)")
    if res["warmup_extras"]:
        by_dom: dict[str, list] = {}
        for e in res["warmup_extras"]:
            by_dom.setdefault(e["domain"], []).append(e["email"])
        for dom, emails in sorted(by_dom.items()):
            L.append(f"- {dom}: {', '.join(sorted(emails))}")
    else:
        L.append("_none_")
    L.append("")

    L.append("## 4. NEEDS PASSWORD — gap mailbox with no credential in the CSV runs")
    L += ([f"- [{e['reason']}] {e['email']} (target {e['target']})" for e in res["no_password"]]
          or ["_none_"])
    L.append("")
    L.append("## 5. UNASSIGNED — couldn't resolve a target workspace")
    L += ([f"- [{e['reason']}] {e['email']}" for e in res["unassigned"]] or ["_none_"])
    L.append("")

    L.append("## Files written")
    L += ([f"- `out/{n}` — {c} accounts" for n, c in written] or ["_none_"])

    report = "\n".join(L) + "\n"
    (OUT_DIR / "report.md").write_text(report, encoding="utf-8")
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-rebuild", action="store_true", help="use existing inventory.json")
    ap.add_argument("--print", dest="dump", action="store_true")
    a = ap.parse_args()

    cfg = load_config()
    if not a.no_rebuild:
        import build_inventory  # noqa: E402  (from DH_SCRIPTS on sys.path)
        dh.save_inventory(build_inventory.build())
    inv = dh.load_inventory()
    cred = build_cred_index(cfg["credential_dirs"])
    res = audit(cfg, inv, cred)
    report = write_outputs(res)

    n_hh = sum(len(v) for v in res["hh_adds"].values())
    n_pv = sum(len(v) for v in res["pv_adds"].values())
    print(f"credentials indexed: {len(cred)}")
    print(f"MUST-FIX add-to-HotHawk: {n_hh}   candidate add-to-PlusVibe: {n_pv}")
    print(f"needs-password: {len(res['no_password'])}   unassigned: {len(res['unassigned'])}"
          f"   warmup-extras(ignored): {len(res['warmup_extras'])}")
    print(f"out dir: {OUT_DIR}")
    if a.dump:
        print("\n" + report)
    return 0


if __name__ == "__main__":
    sys.exit(main())

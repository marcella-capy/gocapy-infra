#!/usr/bin/env python3
"""Give a nameless HotHawk mailbox its BDR display name, by delete + re-add.

Why this is destructive
-----------------------
HotHawk sets a mailbox's Full Name ONLY at creation. Verified against the live API
2026-08-28: PATCH/PUT /mailboxes/{id} 404s, /reconnect rejects name fields (400) and
only replaces credentials, and connect-imap / imap-bulk on an existing address upsert
the CREDENTIALS while silently ignoring the name. The UI cannot edit it either. So the
only way to name an existing mailbox is to delete it and add it back with the name.

That is irreversible without the password, so this script never deletes a mailbox that
has not just proven -- seconds earlier -- that it can log back in.

What a delete costs
-------------------
- Campaign attachments are lost. The re-added mailbox gets a NEW account id and is NOT
  re-attached automatically. This script snapshots every attachment first and rebuilds
  them in phase `attach`. Skipping that silently removes mailboxes from live campaigns.
- campaignDailyLimit resets to the default 10 and CANNOT be restored by API (no request
  schema accepts it). Re-set it in the UI afterwards; `report` prints the list.
- Warmup is NOT affected: it runs on PlusVibe against the underlying SiteGround mailbox,
  not in HotHawk.

Pacing
------
One mailbox per --sleep seconds (default 30). Each mailbox costs ~2 SiteGround logins
(the verify) plus HotHawk's own connect login. Wrong-credential attempts are what get our
sending IP blocklisted, so the script makes ONE verify attempt per mailbox, never retries
a failed login, and hard-stops after --max-consecutive-failures.

Usage
-----
  py rename_mailboxes.py snapshot --out state.json          # read-only, build the plan
  py rename_mailboxes.py run   --state state.json --limit 1 # pilot ONE, then stop
  py rename_mailboxes.py run   --state state.json --yes     # one-at-a-time pass
  py rename_mailboxes.py batch --state state.json --yes     # verify all/delete all/re-add all
  py rename_mailboxes.py attach --state state.json --yes    # rebuild campaign links
  py rename_mailboxes.py verify --state state.json          # read-only check
  py rename_mailboxes.py report --state state.json          # daily-limit to-do list

`run` and `attach` are DRY-RUN unless --yes is passed. The state file is the rollback
record and is rewritten after every single step, so an interrupted run resumes cleanly.

Auth: HOTHAWK_API_TOKEN via the shared capy_env loader.
"""
from __future__ import annotations

import argparse
import csv as csvmod
import imaplib
import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://api.hothawk.ai/v1"
CREDS_ROOT = Path(r"G:\Shared drives\Capy Outreach\Cold Email Accounts")

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

_HERE = Path(__file__).resolve()
for _p in _HERE.parents:
    _cand = _p / "gocapy-claude-plugin" / "go-capy-outreach" / "scripts"
    if (_cand / "capy_env.py").exists():
        sys.path.insert(0, str(_cand))
        break
import capy_env  # noqa: E402


def _client_domains_path() -> Path | None:
    for p in _HERE.parents:
        c = (p / "gocapy-claude-plugin" / "go-capy-outreach" / "shared-references"
             / "voices" / "client-domains.json")
        if c.exists():
            return c
    return None


# --------------------------------------------------------------------------- api

def token() -> str:
    t = capy_env.get("HOTHAWK_API_TOKEN")
    if not t:
        sys.exit("ERROR: HOTHAWK_API_TOKEN not found")
    return t


def req(method: str, path: str, tok: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Authorization": f"Bearer {tok}",
                                        "Content-Type": "application/json"})
    def _parse(raw: bytes):
        """Never let a non-JSON body turn a completed request into a false failure.

        DELETE /mailboxes/{id} returns a bare non-JSON body. Parsing it with a plain
        json.loads raised, the caller saw "failed", and the mailbox was ALREADY deleted
        server-side -- the exact way this script could destroy a mailbox while claiming
        it had not touched it. Status code decides success; the body is best-effort.
        """
        try:
            return json.loads(raw or b"{}")
        except Exception:
            return {"raw": (raw or b"").decode("utf-8", "replace")[:300]}

    try:
        with urllib.request.urlopen(r, timeout=90) as resp:
            return resp.status, _parse(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, _parse(e.read())
    except Exception as e:  # transport failed -- the request may or may not have landed
        return 0, {"message": f"{type(e).__name__}: {e}"}


def paged(path: str, tok: str) -> list[dict]:
    out, page = [], 1
    while True:
        sep = "&" if "?" in path else "?"
        code, d = req("GET", f"{path}{sep}page={page}&take=150", tok)
        if code != 200:
            break
        rows = d.get("data", [])
        out += rows
        if not rows or page >= d.get("meta", {}).get("pagesCount", 1):
            break
        page += 1
    return out


def workspaces(tok):
    _, d = req("GET", "/workspaces/short", tok)
    return d if isinstance(d, list) else d.get("data", [])


# --------------------------------------------------------------------------- names

def capitalise(name: str) -> str:
    """Upper-case the first letter of each part; never lower-case the remainder."""
    return " ".join(w[0].upper() + w[1:] if w and not w[0].isupper() else w
                    for w in (name or "").split())


def _n(s: str) -> str:
    return re.sub(r"[^a-z]", "", (s or "").lower())


def derives(full: str, local: str) -> bool:
    """Does `full` generate `local` under the 5 standard SiteGround prefix forms?"""
    f, _, l = (full or "").strip().partition(" ")
    f, l = _n(f), _n(l)
    if not f or not l:
        return False
    return _n(local) in {f, l, f + l, l + f, f[0] + l, l + f[0], f + l[0], l[0] + f}


def creds_index() -> dict[str, dict]:
    """email -> {name, password}, from the Drive onboarding CSVs.

    SiteGround uses one password per domain, so a mailbox missing from the CSVs can
    still be recovered from a sibling on the same domain (see domain_passwords).
    """
    idx: dict[str, dict] = {}
    if not CREDS_ROOT.exists():
        print(f"warn: {CREDS_ROOT} not reachable -- no passwords available", file=sys.stderr)
        return idx
    for root, _, files in os.walk(CREDS_ROOT):
        for f in files:
            if not f.lower().endswith(".csv"):
                continue
            try:
                with open(os.path.join(root, f), newline="", encoding="utf-8-sig") as fh:
                    for row in csvmod.DictReader(fh):
                        row = {(k or "").strip().lower(): (v or "").strip()
                               for k, v in row.items()}
                        em = (row.get("email") or "").lower()
                        if not em:
                            continue
                        nm = " ".join(x for x in (row.get("first_name"),
                                                  row.get("last_name")) if x).strip()
                        pw = row.get("password") or row.get("smtp_password") or ""
                        cur = idx.setdefault(em, {"name": "", "password": ""})
                        if nm and not cur["name"]:
                            cur["name"] = nm
                        if pw and not cur["password"]:
                            cur["password"] = pw
            except Exception as e:
                print(f"warn: skipped {f}: {type(e).__name__}", file=sys.stderr)
    return idx


def domain_passwords(idx: dict[str, dict]) -> dict[str, str]:
    """domain -> the single password used across that domain, when unambiguous."""
    per: dict[str, set] = {}
    for em, v in idx.items():
        if v["password"] and "@" in em:
            per.setdefault(em.split("@", 1)[1], set()).add(v["password"])
    return {d: s.pop() for d, s in per.items() if len(s) == 1}


def bdr_by_domain() -> dict[str, str]:
    p = _client_domains_path()
    if not p:
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    out = {}
    for client in data.get("clients", {}).values():
        if isinstance(client, dict) and (client.get("bdr") or "").strip():
            for d in client.get("domains", []) or []:
                out[d.strip().lower()] = client["bdr"].strip()
    return out


# --------------------------------------------------------------------------- login

def verify_login(email: str, password: str, host: str) -> tuple[bool, str]:
    """ONE IMAP + ONE SMTP login. Never retried -- a wrong password risks an IP block."""
    ctx = ssl.create_default_context()
    try:
        m = imaplib.IMAP4_SSL(host, 993, ssl_context=ctx, timeout=30)
        try:
            m.login(email, password)
        finally:
            try:
                m.logout()
            except Exception:
                pass
    except Exception as e:
        return False, f"IMAP: {e}"
    try:
        s = smtplib.SMTP_SSL(host, 465, context=ctx, timeout=30)
        try:
            s.login(email, password)
        finally:
            try:
                s.quit()
            except Exception:
                pass
    except Exception as e:
        return False, f"SMTP: {e}"
    return True, "ok"


# --------------------------------------------------------------------------- snapshot

DEFER_PERSONAS = {"Adriana Rodriguez", "Olivia Garcia"}
DEFER_WORKSPACES = {"Capy"}
FALLBACK_DOMAIN_NAMES = {
    "metalaeroparts.com": "Juliana Matos", "advancedforging.com": "Juliana Matos",
    "modularbench.com": "Olivia Garcia", "metalstampedpart.com": "Julia Brooks",
    "metalstampingmfg.com": "Ericka Klein",
}


def cmd_snapshot(args):
    tok = token()
    idx = creds_index()
    dom_pw = domain_passwords(idx)
    bdr = bdr_by_domain()

    ws_list = workspaces(tok)
    ws_by_id = {w["id"]: w["name"] for w in ws_list}
    mailboxes = []
    for w in ws_list:
        for m in paged(f"/mailboxes?workspaceId={w['id']}", tok):
            m["_ws"] = w["name"]
            m["_ws_id"] = w["id"]
            mailboxes.append(m)

    # campaign attachments, keyed by email
    att: dict[str, list] = {}
    campaigns = {}
    for w in ws_list:
        for c in paged(f"/campaigns?workspaceId={w['id']}", tok):
            campaigns[c["id"]] = {"ws": w["name"], "name": c.get("name"),
                                  "status": (c.get("status") or "").lower()}
    for cid, meta in campaigns.items():
        for m in paged(f"/campaigns/{cid}/mailboxes", tok):
            em = (m.get("email") or "").lower()
            if em:
                att.setdefault(em, []).append(cid)

    items, deferred = [], []
    for m in mailboxes:
        em = m["email"].lower()
        if (m.get("fullName") or "").strip():
            continue
        local, dom = em.split("@", 1)
        name = idx.get(em, {}).get("name") or FALLBACK_DOMAIN_NAMES.get(dom) or bdr.get(dom, "")
        name = capitalise(name)
        pw = idx.get(em, {}).get("password") or dom_pw.get(dom, "")
        rec = {
            "email": em, "ws": m["_ws"], "ws_id": m["_ws_id"], "old_id": m["id"],
            "name": name, "password": pw, "host": f"mail.{dom}",
            "campaign_ids": sorted(set(att.get(em, []))),
            "campaign_daily_limit": m.get("campaignDailyLimit"),
            "derives": derives(name, local),
            "status": "pending", "new_id": None, "error": None,
        }
        reason = None
        if m["_ws"] in DEFER_WORKSPACES:
            reason = f"deferred workspace ({m['_ws']})"
        elif name in DEFER_PERSONAS:
            reason = f"deferred persona ({name})"
        elif not name:
            reason = "no name resolvable"
        elif not pw:
            reason = "no password on file"
        elif not rec["derives"]:
            reason = f"name '{name}' does not derive the address"
        if reason:
            rec["status"] = "deferred"
            rec["error"] = reason
            deferred.append(rec)
        else:
            items.append(rec)

    state = {"campaigns": campaigns, "workspaces": ws_by_id,
             "items": items, "deferred": deferred,
             "total_mailboxes": len(mailboxes)}
    Path(args.out).write_text(json.dumps(state, indent=1), encoding="utf-8")

    print(f"mailboxes in HotHawk : {len(mailboxes)}")
    print(f"nameless             : {len(items) + len(deferred)}")
    print(f"  IN SCOPE           : {len(items)}")
    print(f"  deferred           : {len(deferred)}")
    for r in _counter([d["error"] for d in deferred]):
        print(f"      {r}")
    linked = sum(1 for i in items if i["campaign_ids"])
    print(f"in-scope on >=1 campaign: {linked}  "
          f"({sum(len(i['campaign_ids']) for i in items)} links to rebuild)")
    print(f"\nstate written to {args.out}")


def _counter(vals):
    c = {}
    for v in vals:
        c[v] = c.get(v, 0) + 1
    return [f"{v}: {n}" for v, n in sorted(c.items(), key=lambda x: -x[1])]


# --------------------------------------------------------------------------- run

def _save(path, state):
    Path(path).write_text(json.dumps(state, indent=1), encoding="utf-8")


def cmd_run(args):
    tok = token()
    state = json.loads(Path(args.state).read_text(encoding="utf-8"))
    todo = [i for i in state["items"] if i["status"] in ("pending", "verify_failed")]
    if args.limit:
        todo = todo[:args.limit]
    if not todo:
        print("nothing to do -- all items already processed")
        return

    print(f"{'DRY RUN -- ' if not args.yes else ''}{len(todo)} mailbox(es), "
          f"{args.sleep}s apart, ~{len(todo)*args.sleep/60:.0f} min\n")
    consecutive = 0
    for n, item in enumerate(todo, 1):
        em, nm = item["email"], item["name"]
        first, _, last = nm.partition(" ")
        head = f"[{n}/{len(todo)}] {em:42} -> {nm}"
        if not args.yes:
            print(f"{head}   (dry run: verify, delete {item['old_id'][:8]}, re-add, "
                  f"re-attach {len(item['campaign_ids'])} campaign(s))")
            continue

        ok, why = verify_login(em, item["password"], item["host"])
        if not ok:
            item["status"] = "verify_failed"
            item["error"] = why
            _save(args.state, state)
            consecutive += 1
            print(f"{head}\n    SKIPPED, not deleted -- login failed: {why}")
            if consecutive >= args.max_consecutive_failures:
                sys.exit(f"\nSTOPPED: {consecutive} consecutive failures -- "
                         f"possible SiteGround block. Nothing further attempted.")
            time.sleep(args.sleep)
            continue

        code, payload = req("DELETE", f"/mailboxes/{item['old_id']}", tok)
        if code not in (200, 204):
            item["status"] = "delete_failed"
            item["error"] = f"{code} {payload.get('message')}"
            _save(args.state, state)
            consecutive += 1
            print(f"{head}\n    delete failed ({code}) -- mailbox untouched")
            if consecutive >= args.max_consecutive_failures:
                sys.exit("\nSTOPPED: too many consecutive failures.")
            time.sleep(args.sleep)
            continue
        item["status"] = "deleted"
        _save(args.state, state)

        body = {"firstName": first, "lastName": last, "email": em,
                "workspaceId": item["ws_id"],
                "imapUsername": em, "imapPassword": item["password"],
                "imapHost": item["host"], "imapPort": 993,
                "smtpUsername": em, "smtpPassword": item["password"],
                "smtpHost": item["host"], "smtpPort": 465}
        # HotHawk sometimes 400s a re-add of an address it only just deleted -- the
        # record needs a moment to clear. Retrying after a pause fixes it; NOT retrying
        # strands the mailbox deleted, which is the worst outcome this script can produce.
        # This retries the CREATE only (never a failed login), so it cannot hammer auth.
        code, payload = req("POST", "/mailboxes/connect-imap", tok, body)
        for backoff in (5, 15, 30):
            if code in (200, 201):
                break
            print(f"    re-add returned {code}; retrying in {backoff}s "
                  f"(mailbox is currently deleted)")
            time.sleep(backoff)
            code, payload = req("POST", "/mailboxes/connect-imap", tok, body)
        if code in (200, 201):
            item["status"] = "readded"
            item["new_id"] = payload.get("id")
            item["error"] = None
            consecutive = 0
            print(f"{head}\n    OK -- re-added as {str(payload.get('id'))[:8]}")
        else:
            item["status"] = "READD_FAILED"
            item["error"] = f"{code} {payload.get('message')}"
            consecutive += 1
            print(f"{head}\n    *** RE-ADD FAILED ({code}) -- mailbox is DELETED. "
                  f"Password is in the state file; re-run to retry. ***")
        _save(args.state, state)
        if consecutive >= args.max_consecutive_failures:
            sys.exit("\nSTOPPED: too many consecutive failures.")
        if n < len(todo):
            time.sleep(args.sleep)

    done = sum(1 for i in state["items"] if i["status"] == "readded")
    print(f"\n{done}/{len(state['items'])} re-added with names.")
    bad = [i for i in state["items"] if i["status"] not in ("readded", "pending")]
    if bad:
        print("needs attention:")
        for i in bad:
            print(f"  {i['email']:42} {i['status']:14} {i['error']}")


# --------------------------------------------------------------------------- batch

def cmd_batch(args):
    """Verify all, then delete all, pause, then re-add all.

    Why batched: HotHawk 400s a re-add of an address it only just deleted -- every single
    time -- so the one-at-a-time path pays a retry per mailbox. Deleting the whole set and
    pausing before re-adding sidesteps the race entirely.

    The trade is that the whole set is deleted at once instead of one mailbox for a few
    seconds. That is only safe because EVERY login is proven BEFORE the first delete, and
    the state file holds the password, name, host and campaign links for each one -- so a
    re-add can always be retried. A mailbox that fails verification is dropped from the
    batch and never deleted.
    """
    tok = token()
    state = json.loads(Path(args.state).read_text(encoding="utf-8"))
    pending = [i for i in state["items"] if i["status"] == "pending"]
    already_deleted = [i for i in state["items"] if i["status"] in ("deleted", "READD_FAILED")]

    if not args.yes:
        print(f"DRY RUN -- would verify+delete {len(pending)}, wait {args.wait}s, then re-add.")
        print(f"  (plus {len(already_deleted)} already deleted, awaiting re-add)")
        return

    # ---- phase 1: prove every login BEFORE deleting anything -------------------
    verified, failed = [], []
    if pending:
        print(f"PHASE 1/4  verifying {len(pending)} logins, {args.verify_delay}s apart "
              f"(~{len(pending)*args.verify_delay/60:.0f} min). Nothing is deleted yet.")
        for n, i in enumerate(pending, 1):
            ok, why = verify_login(i["email"], i["password"], i["host"])
            if ok:
                verified.append(i)
            else:
                i["status"] = "verify_failed"
                i["error"] = why
                failed.append(i)
                print(f"  [{n}/{len(pending)}] FAIL {i['email']} -- {why[:70]}")
            _save(args.state, state)
            if n < len(pending):
                time.sleep(args.verify_delay)
        print(f"  verified {len(verified)}, failed {len(failed)} "
              f"(failed are dropped from the batch, never deleted)")
        if not verified:
            sys.exit("nothing verified -- aborting before any delete")

    # ---- phase 2: delete the verified set --------------------------------------
    if verified:
        print(chr(10) + f"PHASE 2/4  deleting {len(verified)} mailbox(es)")
        for i in verified:
            code, payload = req("DELETE", f"/mailboxes/{i['old_id']}", tok)
            if code in (200, 204):
                i["status"] = "deleted"
            else:
                i["status"] = "delete_failed"
                i["error"] = f"{code} {payload.get('message') or payload.get('raw')}"
                print(f"  delete failed: {i['email']} ({code}) -- left in place")
            _save(args.state, state)
        print(f"  deleted {sum(1 for i in verified if i['status'] == 'deleted')}")

    # ---- phase 3: let HotHawk release the addresses -----------------------------
    todo = [i for i in state["items"] if i["status"] == "deleted"]
    if todo:
        print(chr(10) + f"PHASE 3/4  waiting {args.wait}s before re-adding "
              f"({len(todo)} mailbox(es) currently deleted)")
        time.sleep(args.wait)

    # ---- phase 4: re-add with the name -----------------------------------------
    print(chr(10) + f"PHASE 4/4  re-adding {len(todo)} mailbox(es) with their names")
    for n, i in enumerate(todo, 1):
        em = i["email"]
        first, _, last = i["name"].partition(" ")
        body = {"firstName": first, "lastName": last, "email": em,
                "workspaceId": i["ws_id"],
                "imapUsername": em, "imapPassword": i["password"],
                "imapHost": i["host"], "imapPort": 993,
                "smtpUsername": em, "smtpPassword": i["password"],
                "smtpHost": i["host"], "smtpPort": 465}
        code, payload = req("POST", "/mailboxes/connect-imap", tok, body)
        for backoff in (5, 15, 30, 60):
            if code in (200, 201):
                break
            time.sleep(backoff)
            code, payload = req("POST", "/mailboxes/connect-imap", tok, body)
        if code in (200, 201):
            i["status"] = "readded"
            i["new_id"] = payload.get("id")
            i["error"] = None
            print(f"  [{n}/{len(todo)}] OK   {em:42} -> {i['name']}")
        else:
            i["status"] = "READD_FAILED"
            i["error"] = f"{code} {payload.get('message') or payload.get('raw')}"
            print(f"  [{n}/{len(todo)}] *** FAILED {em} ({code}) -- STILL DELETED, "
                  f"re-run `batch` to retry ***")
        _save(args.state, state)

    done = sum(1 for i in state["items"] if i["status"] == "readded")
    print(chr(10) + f"{done}/{len(state['items'])} re-added with names.")
    bad = [i for i in state["items"] if i["status"] not in ("readded", "pending")]
    if bad:
        print("needs attention:")
        for i in bad:
            print(f"  {i['email']:42} {i['status']:14} {i['error']}")
    print(chr(10) + "NEXT: run `attach` to put them back on their campaigns.")


# --------------------------------------------------------------------------- attach

def cmd_attach(args):
    tok = token()
    state = json.loads(Path(args.state).read_text(encoding="utf-8"))
    by_campaign: dict[str, list] = {}
    for i in state["items"]:
        if i["status"] == "readded" and i["new_id"]:
            for cid in i["campaign_ids"]:
                by_campaign.setdefault(cid, []).append(i["new_id"])
    if not by_campaign:
        print("no campaign links to rebuild")
        return
    print(f"{'DRY RUN -- ' if not args.yes else ''}"
          f"{len(by_campaign)} campaign(s), "
          f"{sum(len(v) for v in by_campaign.values())} link(s)\n")
    for cid, ids in by_campaign.items():
        meta = state["campaigns"].get(cid, {})
        label = f"{meta.get('ws','?'):16} {str(meta.get('name'))[:46]:48} {len(ids):3} mailbox(es)"
        if not args.yes:
            print(f"  [dry] {label}")
            continue
        code, payload = req("POST", f"/campaigns/{cid}/mailboxes", tok,
                            {"mailboxSelection": {"selectionType": "ids", "accountIds": ids}})
        mark = "OK  " if code in (200, 201) else "FAIL"
        print(f"  [{mark}] {label} ({code}) {payload.get('message') or ''}")


# --------------------------------------------------------------------------- verify

def cmd_verify(args):
    tok = token()
    state = json.loads(Path(args.state).read_text(encoding="utf-8"))
    live = {}
    for w in workspaces(tok):
        for m in paged(f"/mailboxes?workspaceId={w['id']}", tok):
            live[m["email"].lower()] = m
    print(f"mailboxes now: {len(live)}  (snapshot had {state['total_mailboxes']})")
    named = missing = wrong = disc = 0
    for i in state["items"]:
        m = live.get(i["email"])
        if not m:
            missing += 1
            print(f"  MISSING   {i['email']}  (status={i['status']})")
            continue
        full = (m.get("fullName") or "").strip()
        if full != i["name"]:
            wrong += 1
            print(f"  WRONGNAME {i['email']:42} have={full!r} want={i['name']!r}")
        else:
            named += 1
        if m.get("currentConnectionStatus") != "CONNECTED":
            disc += 1
            print(f"  {m.get('currentConnectionStatus'):10} {i['email']}")
    print(f"\ncorrectly named: {named}/{len(state['items'])}"
          f"   missing: {missing}   wrong: {wrong}   not-connected: {disc}")
    blank = sum(1 for m in live.values() if not (m.get("fullName") or "").strip())
    print(f"blank names across all workspaces: {blank}")
    # campaign links
    print("\ncampaign link check:")
    for cid, meta in state["campaigns"].items():
        want = sum(1 for i in state["items"] if cid in i["campaign_ids"])
        if not want:
            continue
        have_ids = {m.get("accountId") or m.get("id") for m in paged(f"/campaigns/{cid}/mailboxes", tok)}
        got = sum(1 for i in state["items"]
                  if cid in i["campaign_ids"] and i.get("new_id") in have_ids)
        mark = "OK " if got == want else "!! "
        print(f"  {mark}{meta.get('ws','?'):16} {str(meta.get('name'))[:44]:46} {got}/{want}")


def cmd_report(args):
    state = json.loads(Path(args.state).read_text(encoding="utf-8"))
    done = [i for i in state["items"] if i["status"] == "readded"]
    print(f"Set campaign daily limit to 19 in the HotHawk UI for these "
          f"{len(done)} mailbox(es):\n")
    by_ws: dict[str, list] = {}
    for i in done:
        by_ws.setdefault(i["ws"], []).append(i)
    for ws in sorted(by_ws):
        print(f"  {ws} ({len(by_ws[ws])})")
        for i in sorted(by_ws[ws], key=lambda x: x["email"]):
            was = i["campaign_daily_limit"]
            note = "" if was in (10, None) else f"   (was {was})"
            print(f"    {i['email']}{note}")


# --------------------------------------------------------------------------- cli

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("snapshot", help="read-only: build the work plan + state file")
    s.add_argument("--out", default="rename_state.json")
    s.set_defaults(func=cmd_snapshot)

    r = sub.add_parser("run", help="verify -> delete -> re-add, one at a time")
    r.add_argument("--state", required=True)
    r.add_argument("--yes", action="store_true", help="actually do it (default dry-run)")
    r.add_argument("--limit", type=int, default=0, help="process at most N (pilot)")
    r.add_argument("--sleep", type=float, default=30.0, help="seconds between mailboxes")
    r.add_argument("--max-consecutive-failures", type=int, default=3)
    r.set_defaults(func=cmd_run)

    b = sub.add_parser("batch", help="verify all -> delete all -> wait -> re-add all")
    b.add_argument("--state", required=True)
    b.add_argument("--yes", action="store_true")
    b.add_argument("--verify-delay", type=float, default=6.0,
                   help="seconds between login checks (SiteGround-safe pace)")
    b.add_argument("--wait", type=float, default=300.0,
                   help="seconds to wait after the deletes before re-adding")
    b.set_defaults(func=cmd_batch)

    a = sub.add_parser("attach", help="rebuild campaign attachments")
    a.add_argument("--state", required=True)
    a.add_argument("--yes", action="store_true")
    a.set_defaults(func=cmd_attach)

    v = sub.add_parser("verify", help="read-only: confirm names, status and links")
    v.add_argument("--state", required=True)
    v.set_defaults(func=cmd_verify)

    p = sub.add_parser("report", help="daily-limit to-do list for the UI")
    p.add_argument("--state", required=True)
    p.set_defaults(func=cmd_report)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

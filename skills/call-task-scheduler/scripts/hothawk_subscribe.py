#!/usr/bin/env python3
"""Workflow B step 2 of call-task-scheduler: push the `to_subscribe` leads from a reconcile
artifact into HotHawk via the REST API (https://api.hothawk.ai/docs/public). MCP tools are the
fallback only if these routes break.

Flow per (workspace, sequence) group — list-based, per the desk's preferred pattern:
  1. find-or-create each lead        GET /crm/leads?workspaceId&search=<email>
                                     POST /crm/leads {workspaceId, email, name, jobTitle,
                                          phoneNumber, companyId}
     (lead create has no companyName field — the company is find-or-created first:
      GET /crm/companies?workspaceId&search=<domain> / POST /crm/companies {workspaceId, name,
      domain}, domain taken from the lead's email)
  2. bulk-add leads to the list      PUT /crm/lists/{id}/leads {selectionType:"ids", leadIds}
     (list find-or-create by name "voicemail-called-<principal>":
      GET /crm/lists?workspaceId / POST /crm/lists {name, workspaceId})
  3. append the list to the campaign POST /campaigns/{sequence_id}/lists
                                     {listSelection: {selectionType:"ids", listIds:[...]}}
     (idempotent — already-attached lists are skipped server-side)
  4. tag the leads "voicemail"       PUT /crm/tags/{id}/leads {selectionType:"ids", leadIds}
     (tag find-or-create: GET /crm/tags?workspaceId / POST /crm/tags {name, workspaceId})

On success this also records the persons in _reconcile_state.json and auto-closes their remaining
open Pipedrive call activities (same effect as reconcile_done_calls.py --mark-subscribed --apply).

CLI:
    python hothawk_subscribe.py --artifact _reconcile_20260707.json [--apply] [--delete-remaining]

Dry-run (default) resolves leads/lists/tags read-only and prints the plan without writing.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import capy_env  # noqa: E402
from reconcile_done_calls import _mark_subscribed  # noqa: E402

BASE = "https://api.hothawk.ai/v1"
TAG_NAME = "voicemail"


def _hh(method: str, path: str, params: "dict | None" = None, body: "dict | None" = None,
        timeout: int = 60, _tries: int = 0):
    token = capy_env.get("HOTHAWK_API_TOKEN")
    if not token:
        sys.exit("ERROR: HOTHAWK_API_TOKEN missing in env")
    url = BASE + path + (("?" + urllib.parse.urlencode(params)) if params else "")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json",
                                          "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        if e.code in (429, 502, 503, 504) and _tries < 3:
            time.sleep(2 * 4 ** _tries)
            return _hh(method, path, params, body, timeout, _tries + 1)
        sys.exit(f"ERROR: HotHawk {method} {path} -> HTTP {e.code}: {detail}\n"
                 f"(REST route may have changed — fall back to the MCP tools per SKILL.md)")
    except (urllib.error.URLError, TimeoutError) as e:
        if _tries < 3:
            time.sleep(2 * 4 ** _tries)
            return _hh(method, path, params, body, timeout, _tries + 1)
        sys.exit(f"ERROR: HotHawk {method} {path} -> connection failed after retries: {e}")


def _rows(r):
    return r.get("data") if isinstance(r, dict) else (r or [])


def _find_lead(ws: str, email: str) -> "dict | None":
    for lead in _rows(_hh("GET", "/crm/leads", {"workspaceId": ws, "search": email, "take": 5})) or []:
        if (lead.get("email") or "").strip().lower() == email.strip().lower():
            return lead
    return None


def _find_or_create_company(ws: str, name: str, domain: str, apply: bool) -> "str | None":
    for c in _rows(_hh("GET", "/crm/companies", {"workspaceId": ws, "search": domain, "take": 10})) or []:
        if (c.get("domain") or "").strip().lower() == domain.lower():
            return c.get("id")
    if not apply:
        return None
    c = _hh("POST", "/crm/companies", body={"workspaceId": ws, "name": name or domain, "domain": domain})
    return c.get("id")


def _find_or_create(kind: str, ws: str, name: str, apply: bool) -> "str | None":
    """kind is 'lists' or 'tags'; match by exact name (case-insensitive)."""
    for row in _rows(_hh("GET", f"/crm/{kind}", {"workspaceId": ws})) or []:
        if (row.get("name") or "").strip().lower() == name.lower():
            return row.get("id")
    if not apply:
        return None
    return _hh("POST", f"/crm/{kind}", body={"name": name, "workspaceId": ws}).get("id")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--artifact", required=True, help="reconcile artifact filename (in scripts/)")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--delete-remaining", action="store_true")
    a = ap.parse_args()

    path = Path(a.artifact) if Path(a.artifact).is_absolute() else HERE / a.artifact
    entries = json.loads(path.read_text(encoding="utf-8-sig")).get("to_subscribe", [])
    if not entries:
        print("RESULT: ok - artifact has no to_subscribe entries")
        return 0

    groups: dict = {}
    for e in entries:
        groups.setdefault((e["workspace_id"], e["sequence_id"], e["principal"]), []).append(e)

    subscribed_pids = []
    for (ws, seq, principal), rows in groups.items():
        list_name = f"voicemail-called-{principal}"
        list_id = _find_or_create("lists", ws, list_name, a.apply)
        tag_id = _find_or_create("tags", ws, TAG_NAME, a.apply)
        lead_ids = []
        for e in rows:
            who = f"{e['first_name']} {e['last_name']} <{e['email']}> ({e['company_name']})"
            lead = _find_lead(ws, e["email"])
            if lead:
                print(f"  found lead {lead['id']}: {who}")
                lead_ids.append(lead["id"])
                subscribed_pids.append(e["person_id"])
                continue
            if not a.apply:
                print(f"  DRY-RUN would create lead: {who}")
                subscribed_pids.append(e["person_id"])
                continue
            domain = e["email"].split("@", 1)[1]
            company_id = _find_or_create_company(ws, e["company_name"], domain, apply=True)
            lead = _hh("POST", "/crm/leads", body={
                "workspaceId": ws,
                "email": e["email"],
                "name": f"{e['first_name']} {e['last_name']}".strip(),
                "jobTitle": e.get("title") or None,
                "phoneNumber": e.get("phone") or None,
                "companyId": company_id,
            })
            # create ignores name-splitting — first/last must be PATCHed on (needed for {{firstName}})
            _hh("PATCH", f"/crm/leads/{lead['id']}",
                body={"firstName": e["first_name"], "lastName": e["last_name"]})
            print(f"  created lead {lead.get('id')}: {who}")
            lead_ids.append(lead["id"])
            subscribed_pids.append(e["person_id"])

        if not a.apply:
            print(f"  DRY-RUN would bulk-add {len(rows)} leads to list "
                  f"'{list_name}' ({list_id or 'to create'}), append list to campaign {seq}, "
                  f"and tag '{TAG_NAME}' ({tag_id or 'to create'})")
            continue

        _hh("PUT", f"/crm/lists/{list_id}/leads", body={"selectionType": "ids", "leadIds": lead_ids})
        print(f"  bulk-added {len(lead_ids)} leads to list '{list_name}' ({list_id})")
        _hh("POST", f"/campaigns/{seq}/lists",
            body={"listSelection": {"selectionType": "ids", "listIds": [list_id]}})
        print(f"  appended list to campaign {seq}")
        _hh("PUT", f"/crm/tags/{tag_id}/leads", body={"selectionType": "ids", "leadIds": lead_ids})
        print(f"  tagged {len(lead_ids)} leads '{TAG_NAME}'")

    if a.apply and subscribed_pids:
        _mark_subscribed(subscribed_pids, a.delete_remaining, apply=True)
    print(f"RESULT: ok - {len(subscribed_pids)} leads "
          f"{'pushed to list/campaign/tag' if a.apply else 'planned (DRY-RUN)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

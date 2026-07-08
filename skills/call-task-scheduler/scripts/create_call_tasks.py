#!/usr/bin/env python3
"""Workflow A of call-task-scheduler: create Pipedrive call activities for a principal's ICP=Yes
people with phone numbers (3 calls: next business day, +5bd, +7bd), and one org note listing the
people WITHOUT phone numbers so the owner can hunt numbers manually.

Reads people from the daily snapshot (pd_cache) — never sweeps Pipedrive live. Writes (activities,
notes) go live via the v2/v1 API.

Title contract (reconcile_done_calls.py parses this — do not change):
    subject = "Call <n>: <Last Name> from <Organization> for <Principal Display Name>"
    The subject IS the machine marker: reconcile matches SUBJECT_RE and resolves the principal by
    display name via the registry. The person is read off the activity's primary participant.

CLI:
    python create_call_tasks.py --principal franklin --org-name "Franklin Casting" \
        --owner-id 23490137 [--org-ids 123,456] [--test] [--apply]

    --principal   principal slug; must exist in references/voicemail-sequences.json
    --org-name    resolve org(s) by name against the snapshot (substring, case-insensitive);
                  ambiguous -> lists matches and exits
    --org-ids     comma-separated Pipedrive org ids (alternative to --org-name)
    --owner-id    Pipedrive user id the activities are assigned to
    --test        create ONE activity for ONE person, print id + URL, stop
    --apply       actually write; default is a dry-run report

Repeat --principal/--org-name|--org-ids pairs are NOT supported in one invocation; run once per
principal (input is one or two principals -> two runs).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
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
OUTREACH_SCRIPTS = Path.home() / ".claude" / "plugins" / "marketplaces" / "gocapy-claude-plugin" / "go-capy-outreach" / "scripts"
sys.path.insert(0, str(OUTREACH_SCRIPTS))  # pd_cache snapshot lives in the outreach plugin
import capy_env  # noqa: E402
import pd_cache  # noqa: E402
from business_days import add_business_days  # noqa: E402

ICP_KEY = "1a8684b9333f530c727f9bff307391d3d200c897"      # Person ICP (Yes/No)
TITLE_KEY = "ef54f66e8242d193fd263fa16ac83850271b2794"    # Person Job Title
LINKEDIN_KEY = "cf2472711fcbe2a22cef32aea82f1a5a555761a8"  # Person LinkedIn Page
ORG_EMAIL_PATTERN_KEY = "3ceb3b7c740bde695671e7cf393cb520e2fa7a65"  # Org Email Pattern
CLAY_WEBHOOKS = HERE.parent / "references" / "clay-webhooks.json"
MIN_CALLABLE_DEFAULT = 5  # orgs with fewer callable people go to the Clay company table
# Subject doubles as the machine marker; group(1) = call number, group(2) = principal display name.
SUBJECT_RE = re.compile(r"^Call ([123]): .+ from .+ for (.+)$")
# Legacy marker (activities created before 2026-07-07 title change) — still honored when matching.
MARKER_RE = re.compile(r"\[call-task-scheduler principal=([a-z0-9-]+) seq=(\d)/3(?: person=\d+)?\]")


def load_registry() -> dict:
    return json.loads(REGISTRY.read_text(encoding="utf-8"))


def principal_of(act: dict, registry: dict) -> "str | None":
    """Slug of the principal a skill-created activity belongs to, or None if not ours.
    Matches the subject contract (preferred) or the legacy note marker."""
    if act.get("type") != "call":
        return None
    m = SUBJECT_RE.match(act.get("subject") or "")
    if m:
        display = m.group(2).strip().lower()
        for slug, entry in registry.items():
            if display in (slug, (entry.get("display_name") or "").lower()):
                return slug
    m = MARKER_RE.search(act.get("note") or "")
    return m.group(1) if m else None
REGISTRY = HERE.parent / "references" / "voicemail-sequences.json"
CALL_OFFSETS = (1, 5, 7)  # business days from today

USERS = {22638704: "Marcella", 20845253: "Jonathan", 20845572: "Sam", 23490137: "Ericka",
         25200747: "Mark"}


# ── live Pipedrive HTTP (writes + activity listing; mirrors pd_cache._pd_get) ──────────────────

def _pd(method: str, path: str, params: "dict | None" = None, body: "dict | None" = None,
        version: str = "v2", timeout: int = 60, _tries: int = 0):
    env = capy_env.load()
    domain, token = env.get("PIPEDRIVE_DOMAIN"), env.get("PIPEDRIVE_API_TOKEN")
    if not domain or not token:
        sys.exit("ERROR: PIPEDRIVE_DOMAIN / PIPEDRIVE_API_TOKEN missing in env")
    qs = {"api_token": token}
    if params:
        qs.update({k: v for k, v in params.items() if v is not None})
    url = f"https://{domain}.pipedrive.com/api/{version}{path}?{urllib.parse.urlencode(qs)}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Accept": "application/json",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        if e.code in (429, 502, 503, 504) and _tries < 4:
            time.sleep(2 * (_tries + 1))
            return _pd(method, path, params, body, version, timeout, _tries + 1)
        sys.exit(f"ERROR: Pipedrive {method} {path} -> HTTP {e.code}: {detail}")
    except (urllib.error.URLError, TimeoutError) as e:
        if _tries < 4:
            time.sleep(2 * (_tries + 1))
            return _pd(method, path, params, body, version, timeout, _tries + 1)
        sys.exit(f"ERROR: Pipedrive {method} {path} -> connection failed after retries: {e}")


def activity_person_id(act: dict) -> "int | None":
    """Person on an activity: primary participant (v2) with person_id fallback."""
    for part in act.get("participants") or []:
        if part.get("primary") and part.get("person_id"):
            return int(part["person_id"])
    for part in act.get("participants") or []:
        if part.get("person_id"):
            return int(part["person_id"])
    pid = act.get("person_id")
    return int(pid) if pid else None


def _open_call_activity_person_ids(registry: dict) -> set:
    """person_ids that already have an OPEN activity created by this skill (v2 cursor pagination;
    v2 has no type/subject filter, so match the subject contract client-side)."""
    ids, cursor = set(), None
    while True:
        r = _pd("GET", "/activities", {"done": "false", "limit": 500, "cursor": cursor})
        for a in r.get("data") or []:
            if principal_of(a, registry):
                pid = activity_person_id(a)
                if pid:
                    ids.add(pid)
        cursor = (r.get("additional_data") or {}).get("next_cursor")
        if not cursor:
            break
    return ids


# ── ICP job-title classifier (mirrors people-icp-classifier/classify_people_icp.py) ────────────
# Default-Yes; positive procurement keywords win over overlapping excludes; empty title -> blank.

ICP_POS_PHRASES = ["supply chain manager", "supply chain", "product development engineer",
                   "program manager", "category manager", "contract manager"]
ICP_POS_WORDS = ["procurement", "sourcing", "supplier", "buyer", "purchasing", "buy", "commodity",
                 "forging", "forged", "forgings", "machining", "machined", "casting", "plastic", "rubber"]
ICP_NEG_PHRASES = ["human resources", "m&a", "e-commerce"]
ICP_NEG_WORDS = ["janitor", "custodian", "babysitter", "machinist", "ceo", "coo", "chief", "marketing",
                 "hr", "inventory", "warehouse", "payroll", "sales", "compliance", "cloud", "digital",
                 "oracle", "cnc", "accounting", "accountant", "designer", "logistics", "logistic",
                 "logisti", "staff", "cybersecurity", "integration", "finance", "indirect", "commercial",
                 "quality", "shipping", "receiving", "human", "welder", "assembly", "assembler",
                 "customer", "service", "mro", "technology", "software", "board", "qa", "business",
                 "account", "financial", "talent", "acquisition", "process", "electronics", "learning",
                 "information", "avionics", "structures", "stress", "field", "fleet", "transportation",
                 "technician", "foreman", "control", "cost", "intern", "schedule", "traffic", "freight",
                 "workers", "recruiter", "capital", "capex", "expeditor", "test", "repair", "flight",
                 "handler", "scheduling", "aftermarket", "systems", "airport", "scheduler", "crew",
                 "electrical", "substation", "transformation", "building", "training", "education",
                 "investor", "climate", "medical", "culture", "delivery", "pmo", "data", "analytics",
                 "commerce", "facilities", "composites", "engineering", "maintenance", "administrator",
                 "assistant", "developer", "additive", "raw", "cyber", "it", "chemicals", "contractor"]


def classify_title(title: str) -> str:
    """'Yes' / 'No' / '' (blank for empty title)."""
    t = (title or "").strip().lower()
    if not t:
        return ""

    def phrase_hit(ph):
        return re.search(r"(?<![a-z0-9])" + re.escape(ph) + r"(?![a-z0-9])", t) is not None

    tokens = set(re.findall(r"[a-z0-9]+", t))
    if any(phrase_hit(ph) for ph in ICP_POS_PHRASES) or tokens & set(ICP_POS_WORDS):
        return "Yes"
    if any(phrase_hit(ph) for ph in ICP_NEG_PHRASES) or tokens & set(ICP_NEG_WORDS):
        return "No"
    return "Yes"


# ── snapshot helpers ────────────────────────────────────────────────────────────────────────────

def _resolve_orgs(org_name: "str | None", org_ids: "str | None") -> "dict[str, dict]":
    orgs = pd_cache.get_orgs()
    if org_ids:
        wanted = {s.strip() for s in org_ids.split(",") if s.strip()}
        found = {i: orgs[i] for i in wanted if i in orgs}
        missing = wanted - set(found)
        if missing:
            sys.exit(f"ERROR: org ids not in snapshot: {sorted(missing)}")
        return found
    needle = (org_name or "").strip().lower()
    if not needle:
        sys.exit("ERROR: provide --org-name or --org-ids")
    hits = {i: o for i, o in orgs.items() if needle in (o.get("name") or "").lower()}
    if not hits:
        sys.exit(f"ERROR: no snapshot org matches '{org_name}'")
    if len(hits) > 1:
        for i, o in sorted(hits.items(), key=lambda kv: kv[1].get("name") or ""):
            print(f"  {i}: {o.get('name')}")
        sys.exit(f"ERROR: '{org_name}' is ambiguous ({len(hits)} matches above); use --org-ids")
    return hits


def _phones(p: dict) -> list:
    return [ph.get("value").strip() for ph in (p.get("phone") or [])
            if isinstance(ph, dict) and (ph.get("value") or "").strip()]


def _primary_email(p: dict) -> str:
    for e in (p.get("email") or []):
        if isinstance(e, dict) and (e.get("value") or "").strip():
            return e["value"].strip()
    return ""


def _split_name(p: dict) -> "tuple[str, str]":
    first = (p.get("first_name") or "").strip()
    last = (p.get("last_name") or "").strip()
    if first or last:
        return first, last
    parts = (p.get("name") or "").strip().split()
    return (" ".join(parts[:-1]), parts[-1]) if len(parts) > 1 else (parts[0] if parts else "", "")


def _clay_post(url: str, payload: dict, timeout: int = 30, _tries: int = 0) -> bool:
    """POST one row to a Clay webhook table. Returns True on 2xx."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        if e.code in (429, 502, 503, 504) and _tries < 3:
            time.sleep(2 * (_tries + 1))
            return _clay_post(url, payload, timeout, _tries + 1)
        sys.stderr.write(f"WARN: Clay POST {url[-20:]} -> HTTP {e.code}\n")
        return False
    except (urllib.error.URLError, TimeoutError) as e:
        if _tries < 3:
            time.sleep(2 * (_tries + 1))
            return _clay_post(url, payload, timeout, _tries + 1)
        sys.stderr.write(f"WARN: Clay POST failed after retries: {e}\n")
        return False


def _org_domain(org: dict) -> str:
    w = (org.get("website") or "").strip()
    return w.replace("https://", "").replace("http://", "").replace("www.", "").rstrip("/")


def _last_name(p: dict) -> str:
    ln = (p.get("last_name") or "").strip()
    if ln:
        return ln
    parts = (p.get("name") or "").strip().split()
    return parts[-1] if parts else "?"


# ── main ────────────────────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--principal", required=True, help="principal slug (registry key)")
    ap.add_argument("--org-name", help="resolve org by name from the snapshot")
    ap.add_argument("--org-ids", help="comma-separated Pipedrive org ids")
    ap.add_argument("--owner-id", required=True, type=int, help="Pipedrive user id for the activities")
    ap.add_argument("--display-name", help="principal display name for the title (default: org name)")
    ap.add_argument("--test", action="store_true", help="create ONE activity for ONE person, then stop")
    ap.add_argument("--apply", action="store_true", help="write to Pipedrive (default: dry-run)")
    ap.add_argument("--min-callable", type=int, default=MIN_CALLABLE_DEFAULT,
                    help="orgs with fewer ICP-Yes people with phones get pushed to the Clay company table")
    a = ap.parse_args()

    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    if a.principal not in registry:
        sys.exit(f"ERROR: principal '{a.principal}' not in {REGISTRY.name} — ask the user for the "
                 f"HotHawk campaign URL, add the entry, then re-run")

    orgs = _resolve_orgs(a.org_name, a.org_ids)
    display = a.display_name or registry[a.principal].get("display_name") or a.principal
    persons = pd_cache.persons_by_org_ids(list(orgs))

    # classify blank-ICP people from their job title; write the verdict back on --apply
    classified = 0
    for p in persons:
        if str(p.get(ICP_KEY) or "").strip():
            continue
        verdict = classify_title(p.get(TITLE_KEY) or "")
        if not verdict:
            continue
        if a.apply:
            _pd("PUT", f"/persons/{p['id']}", body={ICP_KEY: verdict}, version="v1")
            pd_cache.apply_local_write("persons", p["id"], {ICP_KEY: verdict})
        p[ICP_KEY] = verdict
        classified += 1
    if classified:
        print(f"  ICP-classified {classified} blank people from job titles"
              + ("" if a.apply else " (DRY-RUN, not written back)"))

    icp_yes = [p for p in persons if str(p.get(ICP_KEY) or "").strip().lower() == "yes"]

    already = _open_call_activity_person_ids(registry) if (a.apply or icp_yes) else set()

    # duplicate-record guard: same human twice at one org (same normalized name) -> keep ONE
    # record per human. Preference: already has open tasks (idempotency) > has email > more
    # phones > older id. Losing duplicates are reported so they can be merged later.
    by_human: dict = {}
    dup_pairs = []
    for p in icp_yes:
        # last name + first initial, so "Reggie West" and "Reginald West" collide too
        first, last = _split_name(p)
        key = re.sub(r"[^a-z]", "", last.lower()) + "|" + (first[:1].lower() if first else "")
        cur = by_human.get(key)
        if cur is None:
            by_human[key] = p
            continue
        def score(x):
            return (int(x["id"]) in already, bool(_primary_email(x)), len(_phones(x)), -int(x["id"]))
        if score(p) > score(cur):
            dup_pairs.append((p, cur))
            by_human[key] = p
        else:
            dup_pairs.append((cur, p))
    icp_yes = list(by_human.values())
    for kept, ignored in dup_pairs:
        print(f"  duplicate record ignored: {ignored.get('name')} (id {ignored['id']}) "
              f"-> tasks only on id {kept['id']}")

    with_phone = [p for p in icp_yes if _phones(p)]
    no_phone = [p for p in icp_yes if not _phones(p)]

    print(f"principal={a.principal} orgs={[o.get('name') for o in orgs.values()]} "
          f"persons={len(persons)} icp_yes={len(icp_yes)} with_phone={len(with_phone)} "
          f"no_phone={len(no_phone)} dupes_ignored={len(dup_pairs)} "
          f"owner={USERS.get(a.owner_id, a.owner_id)}")
    # also skip anyone already pushed to a HotHawk sequence by the reconcile
    state_file = HERE / "_reconcile_state.json"
    if state_file.exists():
        try:
            already |= {int(k) for k in json.loads(state_file.read_text(encoding="utf-8-sig"))}
        except (json.JSONDecodeError, OSError, ValueError):
            pass
    todo = [p for p in with_phone if int(p["id"]) not in already]
    skipped = len(with_phone) - len(todo)
    if a.test:
        if skipped:
            # test mode is a first-run trial; if this skill already has open call tasks here,
            # re-running the test should prove idempotency, not move on to the next person
            print(f"RESULT: ok - TEST skipped: {skipped} person(s) already have open call tasks")
            return 0
        todo = todo[:1]

    today = _dt.date.today()
    due_dates = [add_business_days(today, n) for n in CALL_OFFSETS]
    seq_count = 1 if a.test else 3

    plan = []
    for p in todo:
        org_rel = p.get("org_id")
        oid = org_rel.get("value") if isinstance(org_rel, dict) else org_rel
        org_name = orgs.get(str(oid), {}).get("name") or p.get("org_name") or ""
        for n in range(1, seq_count + 1):
            note = f"Call {p.get('name')} - {p.get(TITLE_KEY) or 'no title'} @ {', '.join(_phones(p))}"
            if n == 1:  # only the first done call adds the person to the sequence
                note += '<br><br>[Clicking "Mark As Done" moves lead to a Voicemail Email Sequence in HotHawk]'
            plan.append({
                "subject": f"Call {n}: {_last_name(p)} from {org_name} for {display}",
                "type": "call",
                "owner_id": a.owner_id,
                "person_id": int(p["id"]),  # bookkeeping only — sent as participants (v2 rule)
                "org_id": int(oid) if oid else None,
                "due_date": due_dates[n - 1].isoformat(),
                "note": note,
            })

    if not a.apply:
        for item in plan:
            print(f"  DRY-RUN activity: {item['subject']} due {item['due_date']} person {item['person_id']}")
        if no_phone:
            print(f"  DRY-RUN note on org(s) for {len(no_phone)} people without phones: "
                  + "; ".join(f"{p.get('name')} ({p.get(TITLE_KEY) or 'no title'})" for p in no_phone))
        print(f"RESULT: ok - DRY-RUN {len(todo)} persons, {len(plan)} activities planned, "
              f"{skipped} skipped-existing, {len(no_phone)} no-phone")
        return 0

    env = capy_env.load()
    domain = env.get("PIPEDRIVE_DOMAIN", "capy")
    created = []
    for item in plan:
        body = {k: v for k, v in item.items() if v is not None and k != "person_id"}
        body["participants"] = [{"person_id": item["person_id"], "primary": True}]
        r = _pd("POST", "/activities", body=body)
        aid = (r.get("data") or {}).get("id")
        created.append({"activity_id": aid, "person_id": item["person_id"], "subject": item["subject"],
                        "due_date": item["due_date"]})
        print(f"  created activity {aid}: {item['subject']} due {item['due_date']} "
              f"https://{domain}.pipedrive.com/activities/list (id={aid})")

    note_ids = []
    if no_phone and not a.test:
        by_org: dict = {}
        for p in no_phone:
            rel = p.get("org_id")
            oid = rel.get("value") if isinstance(rel, dict) else rel
            by_org.setdefault(oid, []).append(p)
        owner_name = USERS.get(a.owner_id, str(a.owner_id))
        for oid, people in by_org.items():
            header = f"<b>Needs phone numbers for {display} call sequence — @{owner_name}</b>"
            existing = _pd("GET", "/notes", {"org_id": int(oid), "limit": 100}, version="v1")
            if any(header in (n.get("content") or "") for n in existing.get("data") or []):
                print(f"  note already exists on org {oid} — skipping duplicate")
                continue
            rows = "".join(f"<li>{p.get('name')} — {p.get(TITLE_KEY) or 'no title'}</li>" for p in people)
            r = _pd("POST", "/notes", body={"content": header + f"<ul>{rows}</ul>", "org_id": int(oid)},
                    version="v1")
            nid = (r.get("data") or {}).get("id")
            note_ids.append(nid)
            print(f"  created note {nid} on org {oid} ({len(people)} people without phones)")

    # Clay enrichment pushes (skip in test mode): no-phone people -> People table;
    # orgs short on callable people -> Company table (so Clay can source more people).
    clay_people = clay_companies = 0
    if not a.test:
        clay = json.loads(CLAY_WEBHOOKS.read_text(encoding="utf-8"))
        for p in no_phone:
            first, last = _split_name(p)
            rel = p.get("org_id")
            oid = rel.get("value") if isinstance(rel, dict) else rel
            org = orgs.get(str(oid), {})
            payload = {
                "name": p.get("name"), "first_name": first, "last_name": last,
                "job_title": p.get(TITLE_KEY) or "",
                "email": _primary_email(p),
                "linkedin_url": p.get(LINKEDIN_KEY) or "",
                "company_name": org.get("name") or p.get("org_name") or "",
                "company_domain": ((_primary_email(p).split("@", 1) + [""])[1]),
                "pipedrive_person_id": int(p["id"]),
                "pipedrive_org_id": int(oid) if oid else None,
            }
            if _clay_post(clay["people_no_phone"], payload):
                clay_people += 1
        for oid, org in orgs.items():
            callable_n = sum(1 for p in with_phone
                             if str((p.get("org_id") or {}).get("value")
                                    if isinstance(p.get("org_id"), dict) else p.get("org_id")) == oid)
            if callable_n < a.min_callable:
                org_payload = {
                    "company_name": org.get("name") or "",
                    "company_domain": _org_domain(org),
                    "email_pattern": str(org.get(ORG_EMAIL_PATTERN_KEY) or ""),
                    "pipedrive_org_id": int(oid),
                    "icp_yes_count": len(icp_yes), "with_phone_count": callable_n,
                    "no_phone_count": len(no_phone),
                }
                if _clay_post(clay["companies_need_people"], org_payload):
                    clay_companies += 1
                    print(f"  pushed org {org.get('name')} to Clay company table "
                          f"({callable_n} callable < {a.min_callable})")
        if clay_people:
            print(f"  pushed {clay_people} no-phone people to Clay people table")

    ledger = {"date": today.isoformat(), "principal": a.principal, "owner_id": a.owner_id,
              "test": a.test, "activities": created, "note_ids": note_ids,
              "skipped_existing": skipped, "clay_people": clay_people, "clay_companies": clay_companies,
              "no_phone": [{"person_id": p["id"], "name": p.get("name")} for p in no_phone]}
    out = HERE / f"_{a.principal}_calltasks_{today.strftime('%Y%m%d')}.json"
    out.write_text(json.dumps(ledger, indent=2), encoding="utf-8")

    mode = "TEST " if a.test else ""
    print(f"RESULT: ok - {mode}{len(todo)} persons, {len(created)} activities created, "
          f"{skipped} skipped-existing, {len(no_phone)} no-phone noted ({len(note_ids)} notes), "
          f"clay: {clay_people} people / {clay_companies} companies")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

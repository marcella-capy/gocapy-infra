#!/usr/bin/env python3
"""
domain-inventory skill — consolidated domain registry across Porkbun + SiteGround + GoDaddy,
grouped by principal, with expiration / renewal status / annual cost.

Porkbun is pulled LIVE via API (read-only: ping, domain/listAll, pricing/get).
SiteGround + GoDaddy have no usable list API, so their rows are supplied via --extra
(a normalized CSV the agent builds from the dashboard export / screenshots).

Usage:
  python domain_inventory.py [--extra extra.csv] [--out domain_inventory.csv]
                             [--client-domains PATH] [--overrides PATH]

--extra CSV header: domain,registrar,expiration,renewal,cost
  - expiration as YYYY-MM-DD (or "Mon DD, YYYY" — auto-parsed)
  - renewal: ON | OFF | unknown
  - cost: blank -> filled from overrides.prices by registrar

Exit codes: 0 ok, 2 Porkbun creds missing, 3 Porkbun API error (SiteGround/GoDaddy still emit).
"""
import argparse, csv, datetime, json, os, sys, urllib.request
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
ENV = Path.home() / ".claude" / "global.env"


def log(*a):
    print(*a, file=sys.stderr)


# ---------- config / creds ----------
def load_env():
    d = {}
    if ENV.exists():
        for line in ENV.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                d[k.strip()] = v.strip()
    # env vars win if already exported
    for k in ("PORKBUN_API_KEY", "PORKBUN_SECRET_KEY"):
        if os.environ.get(k):
            d[k] = os.environ[k]
    return d


def find_client_domains(explicit):
    if explicit:
        return Path(explicit)
    # search up from the skill toward the marketplaces root
    rel = Path("gocapy-claude-plugin/go-capy-outreach/shared-references/voices/client-domains.json")
    for base in [SKILL, *SKILL.parents]:
        cand = base / rel
        if cand.exists():
            return cand
    # also try a sibling marketplaces layout
    for p in SKILL.parents:
        for cand in p.glob("**/shared-references/voices/client-domains.json"):
            return cand
    return None


def iso(s):
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s[:10]


# ---------- principal resolution ----------
def build_resolver(client_domains_path, ov):
    dom2principal = {}
    if client_domains_path and client_domains_path.exists():
        cd = json.loads(client_domains_path.read_text(encoding="utf-8"))
        disp = ov.get("slug_display", {})
        for slug, info in cd.get("clients", {}).items():
            for d in info.get("domains", []):
                dom2principal[d.lower()] = disp.get(slug, slug)
    else:
        log("WARN: client-domains.json not found — relying on overrides + keyword fallback only")

    overrides = {k.lower(): v for k, v in ov.get("domain_overrides", {}).items() if not k.startswith("_")}
    hints = tuple(ov.get("internal_hints", []))
    rules = ov.get("keyword_fallback", {}).get("rules", [])

    def resolve(domain):
        d = domain.lower()
        if d in overrides:
            return overrides[d]
        if d in dom2principal:
            return dom2principal[d]
        if any(h in d for h in hints):
            return "Internal / Capy"
        for kw, name in rules:
            if kw in d:
                return name
        return "Unassigned"

    return resolve


# ---------- Porkbun ----------
def post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def pull_porkbun(env, price_fallback):
    api, sec = env.get("PORKBUN_API_KEY"), env.get("PORKBUN_SECRET_KEY")
    if not api or not sec:
        return [], "MISSING_CREDS"
    try:
        ping = post("https://api.porkbun.com/api/json/v3/ping", {"apikey": api, "secretapikey": sec})
        if ping.get("status") != "SUCCESS":
            return [], f"ping failed: {ping}"
        prices = post("https://api.porkbun.com/api/json/v3/pricing/get", {}).get("pricing", {})
        rows, start = [], 0
        while True:
            r = post("https://api.porkbun.com/api/json/v3/domain/listAll",
                     {"apikey": api, "secretapikey": sec, "start": start})
            if r.get("status") != "SUCCESS":
                return rows, f"listAll failed: {r}"
            batch = r.get("domains", [])
            for x in batch:
                tld = x.get("tld") or x["domain"].split(".")[-1]
                renew = prices.get(tld, {}).get("renewal")
                cost = float(renew) if renew else price_fallback
                on = str(x.get("autoRenew")) in ("1", "yes", "true")
                rows.append({
                    "domain": x["domain"], "registrar": "Porkbun",
                    "expiration": (x.get("expireDate", "") or "")[:10],
                    "renewal": "ON" if on else "OFF",
                    "_cost": cost, "cost": f"${cost:.2f}", "flag": "",
                })
            if len(batch) < 1000:
                break
            start += 1000
        return rows, None
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"


# ---------- extra (SiteGround / GoDaddy) ----------
def load_extra(path, prices):
    rows = []
    if not path:
        return rows
    p = Path(path)
    if not p.exists():
        log(f"WARN: --extra file not found: {p}")
        return rows
    with open(p, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            dom = (r.get("domain") or "").strip().lower()
            if not dom:
                continue
            reg = (r.get("registrar") or "").strip() or "Unknown"
            cost_raw = (r.get("cost") or "").strip().lstrip("$")
            if cost_raw:
                cost = float(cost_raw)
            else:
                cost = prices.get(reg.lower(), 0.0)
            rows.append({
                "domain": r["domain"].strip(), "registrar": reg,
                "expiration": iso(r.get("expiration")),
                "renewal": (r.get("renewal") or "unknown").strip() or "unknown",
                "_cost": cost, "cost": f"${cost:.2f}", "flag": "",
            })
    return rows


# ---------- main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--extra", help="CSV of SiteGround/GoDaddy rows: domain,registrar,expiration,renewal,cost")
    ap.add_argument("--out", default=str(Path.cwd() / "domain_inventory.csv"))
    ap.add_argument("--client-domains")
    ap.add_argument("--overrides", default=str(SKILL / "references" / "principal-overrides.json"))
    args = ap.parse_args()

    ov = json.loads(Path(args.overrides).read_text(encoding="utf-8"))
    prices = ov.get("prices", {})
    extra_prices = {"siteground": prices.get("siteground", 0.0), "godaddy": prices.get("godaddy", 0.0)}
    resolve = build_resolver(find_client_domains(args.client_domains), ov)
    capy_principals = set(ov.get("capy_principals", ["Internal / Capy"]))

    porkbun, pk_err = pull_porkbun(load_env(), prices.get("porkbun_fallback", 11.08))
    if pk_err == "MISSING_CREDS":
        log("ERROR: PORKBUN_API_KEY / PORKBUN_SECRET_KEY not set in ~/.claude/global.env")
    elif pk_err:
        log(f"WARN: Porkbun pull incomplete: {pk_err}")
    extra = load_extra(args.extra, extra_prices)

    # merge + dedupe (note dual registrar if a domain appears twice)
    by_dom = {}
    for r in porkbun + extra:
        d = r["domain"].lower()
        if d in by_dom:
            by_dom[d]["registrar"] += " + " + r["registrar"]
        else:
            by_dom[d] = dict(r)
    rows = list(by_dom.values())
    for r in rows:
        r["principal"] = resolve(r["domain"])

    # write CSV (sorted by principal, then expiration)
    rows.sort(key=lambda x: (x["principal"], x["expiration"] or "9999"))
    out = Path(args.out)
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["domain", "principal", "registrar", "expiration", "renewal", "cost"])
        w.writeheader()
        for r in rows:
            w.writerow({k: r[k] for k in ["domain", "principal", "registrar", "expiration", "renewal", "cost"]})

    # console summary
    groups = defaultdict(list)
    for r in rows:
        groups[r["principal"]].append(r)

    def block(title, names):
        sub = 0.0
        print(f"\n=== {title} ===")
        for g in sorted(names, key=lambda n: -sum(x["_cost"] for x in groups[n])):
            items = groups[g]
            gsub = sum(x["_cost"] for x in items)
            sub += gsub
            offs = sum(1 for x in items if x["renewal"] == "OFF")
            note = f"  ({offs} renew=OFF)" if offs else ""
            print(f"  {g:<42} {len(items):>3} domains   ${gsub:>8.2f}/yr{note}")
        print(f"  {'-'*42} {'':>3}           ${sub:>8.2f}/yr")
        return sub

    client_names = [n for n in groups if n not in capy_principals and not n.startswith("DROP")]
    drop_names = [n for n in groups if n.startswith("DROP")]
    capy_names = [n for n in groups if n in capy_principals]

    print(f"Porkbun: {len(porkbun)}   Extra(SG/GoDaddy): {len(extra)}   Total unique: {len(rows)}"
          + (f"   PORKBUN_ERROR={pk_err}" if pk_err else ""))
    c = block("CLIENTS", client_names)
    d = block("DROP / FORMER (let lapse)", drop_names) if drop_names else 0.0
    k = block("CAPY (internal / corporate / product)", capy_names) if capy_names else 0.0
    un = groups.get("Unassigned")
    if un:
        print(f"\n!! UNASSIGNED ({len(un)}): " + ", ".join(x["domain"] for x in un))
    print(f"\nGRAND TOTAL: ${c + d + k:.2f}/yr across {len(rows)} domains")
    print(f"Excluding renew=OFF: ${sum(x['_cost'] for x in rows if x['renewal'] != 'OFF'):.2f}/yr")
    print(f"\nCSV -> {out}")
    return 2 if pk_err == "MISSING_CREDS" else 0


if __name__ == "__main__":
    sys.exit(main())

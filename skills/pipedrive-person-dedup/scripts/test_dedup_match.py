#!/usr/bin/env python3
"""Fixture tests for the dedup matchers (persons + orgs + merge repair). No Pipedrive, no snapshot.
Run:  python test_dedup_match.py      (exit 0 = all pass)"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent / "pipedrive-org-dedup" / "scripts"))

import dedup_common as dc  # noqa: E402
import dedup_orgs as org  # noqa: E402
import dedup_persons as per  # noqa: E402

per.NICK.update(per.load_nicknames())
N = per.Name
FAILS = []


def check(label, got, want):
    if got != want:
        FAILS.append(f"{label}: got {got!r}, want {want!r}")


# -- person names -------------------------------------------------------------------------
check("exact", per.names_agree(N("John Smith"), N("john smith")), 2)
check("suffix/credential", per.names_agree(N("Ray Metzler, PMP"), N("Ray Metzler")), 2)
check("jr + dr", per.names_agree(N("Dr. Alan Wood Jr."), N("Alan Wood")), 2)
check("accents", per.names_agree(N("José Núñez"), N("Jose Nunez")), 2)
check("nickname Bob/Robert", per.names_agree(N("Bob Moncada"), N("Robert Moncada")), 1)
check("nickname Mike/Michael", per.names_agree(N("Mike Hall"), N("Michael Hall")), 1)
check("abbrev last", per.names_agree(N("Lisa S."), N("Lisa Sampson")), 1)
check("initial first", per.names_agree(N("J. Smith"), N("John Smith")), 1)
check("compound surname", per.names_agree(N("Cesar Perez Morelos"), N("Cesar Perez")), 1)
check("prefix Chris", per.names_agree(N("Chris Weik"), N("Christopher Weik")), 1)
check("swapped", per.names_agree(N("Fairall Paul"), N("Paul Fairall")), 1)
check("different people same last", per.names_agree(N("Brandon Blanchard"), N("Brandon Taylor")), 0)
check("different first", per.names_agree(N("Courtney Spencer"), N("Tom Spencer")), 0)
check("wrong initial", per.names_agree(N("Lisa T."), N("Lisa Sampson")), 0)
check("junk vs real", per.names_agree(N("moncy matacz"), N("Philip Serra")), 0)
check("Name.full abbreviated", N("Lisa S.").full, False)

# -- org names ----------------------------------------------------------------------------
check("org same w/ suffix", org.name_strength("Bescast, Inc.", "Bescast"), 2)
check("org punctuation", org.name_strength("A&A Industries", "AandA Industries"), 2)
check("org typo", org.name_strength("Earle M. Jorgensen Company", "Earle M Jorgenson"), 2)
check("org division", org.name_strength("Airbus", "Airbus Helicopter"), 1)
check("org parent+brand", org.name_strength("Diebold Inc. (OH)", "Diebold Nixdorf"), 1)
check("org sister divisions", org.name_strength("Teledyne Reynolds", "Teledyne Qioptiq"), 0)
check("org different", org.name_strength("Idaho Power", "Idaho First Bank"), 0)
check("org acquired", org.name_strength("AIM Aviation of Auburn", "Sekisui Aerospace"), 0)

# -- LinkedIn slugs -----------------------------------------------------------------------
check("li company", dc.linkedin_slug("https://de.linkedin.com/company/Acme-Corp/?x=1", "company"), "acme-corp")
check("li person", dc.linkedin_slug("linkedin.com/in/john-doe-123/", "in"), "john-doe-123")
check("li wrong kind", dc.linkedin_slug("https://www.linkedin.com/in/jd", "company"), "")

# -- merge repair: restore joined text, leave fills and names/phones alone ------------------
K = "cf2472711fcbe2a22cef32aea82f1a5a555761a8"
V = "e6314b70c8ff11d9d68ef9cf6a58c96cae10dfb3"
before = {"name": "Lisa Sampson", K: "https://www.linkedin.com/in/ls", V: "ok",
          "job_title": "Buyer", "phone": "555"}
after = {"name": "Lisa Sampson, Lisa S.", K: "https://www.linkedin.com/in/ls, https://www.linkedin.com/in/ls/",
         V: "ok, valid", "job_title": "Buyer", "phone": "555, 777",
         "63edeb330eeb2cf8c3813691af27bcbc702373ee": "filled from loser"}
fix = dc.joined_fields(before, after)
check("repair set", sorted(fix), sorted([K, V]))
check("repair value", fix.get(V), "ok")
check("repair never name/phone", bool(set(fix) & dc.NEVER_WRITE), False)

if FAILS:
    print("FAIL\n  " + "\n  ".join(FAILS))
    sys.exit(1)
print("all dedup matcher tests passed")

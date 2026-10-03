"""Build companies_yc.toml from Y Combinator companies that are hiring in the US.

    python scripts/import_yc.py              # discover boards (~10 min) and write companies_yc.toml
    python scripts/import_yc.py --found f.json   # reuse a previous run's hits

The company list comes from the open YC directory at yc-oss.github.io. Each company's board is
looked for on Ashby, Greenhouse, Lever and Workable, and a hit is kept only if the board visibly
belongs to that company: its name or web domain appears in the board's company name (Greenhouse,
Workable) or in a posting's text (Ashby, Lever). That rejects namesakes, like a Dutch hotel group
whose Recruitee board is also called "clay".
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from jobpoller import http  # noqa: E402
from jobpoller.ats import FETCHERS  # noqa: E402
from jobpoller.discover import slug_candidates  # noqa: E402

YC_HIRING = "https://yc-oss.github.io/api/companies/hiring.json"
US_REGIONS = {"United States of America", "America / Canada", "Remote", "Fully Remote", "Partly Remote"}


def norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def domain(c: dict) -> str:
    return urllib.parse.urlparse(c.get("website") or "").netloc.removeprefix("www.").split(".")[0].lower()


def clean_name(name: str) -> str:
    return re.sub(r"[^\w\s.&'+-]", "", name).strip()  # "Dots 💸" -> "Dots"


def belongs(c: dict, text: str) -> bool:
    t, name, dom = norm(text), norm(clean_name(c["name"])), norm(domain(c))
    if len(name) < 6:  # short names hide inside other words ("arc" in "research"): whole words only
        host = urllib.parse.urlparse(c.get("website") or "").netloc.removeprefix("www.").lower()
        word = rf"(?<![a-z0-9]){re.escape(clean_name(c['name']).lower())}(?![a-z0-9])"
        return bool(re.search(word, text.lower()) or (host and host in text.lower()))
    return name in t or (len(dom) >= 6 and dom in t)


def try_board(c: dict, ats: str, slug: str) -> dict | None:
    q = urllib.parse.quote(slug)
    try:
        if ats == "ashby":
            jobs = [j for j in http.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{q}").get("jobs", [])
                    if j.get("isListed", True)]
            text = " ".join((j.get("descriptionPlain") or "")[:3000] for j in jobs[:3])
        elif ats == "greenhouse":
            jobs = http.get_json(f"https://boards-api.greenhouse.io/v1/boards/{q}/jobs").get("jobs", [])
            text = " ".join(j.get("company_name") or "" for j in jobs[:3])
        elif ats == "lever":
            jobs = http.get_json(f"https://api.lever.co/v0/postings/{q}?mode=json&limit=5")
            text = " ".join((j.get("descriptionPlain") or "") + (j.get("additionalPlain") or "") for j in jobs[:3])
        else:
            d = http.get_json(f"https://apply.workable.com/api/v1/widget/accounts/{q}")
            jobs, text = d.get("jobs", []), d.get("name") or ""
    except Exception:  # noqa: BLE001 - a miss is the common case
        return None
    return {"ats": ats, "slug": slug, "count": len(jobs), "verified": belongs(c, text)} if jobs else None


PREVIOUS: set[str] = set()  # names already in the output file, filled in by main()


def find(c: dict) -> dict | None:
    slugs = list(dict.fromkeys([c["slug"], *slug_candidates(c["name"]), domain(c)]))
    first = None
    for ats in ("ashby", "greenhouse", "lever", "workable"):
        for s in slugs[:4] if ats != "workable" else slugs[:1]:  # Workable rate-limits; one try
            hit = try_board(c, ats, s)
            if hit and hit["verified"]:
                return {**hit, "name": c["name"], "batch": c.get("batch"), "industry": c.get("industry")}
            first = first or (hit and {**hit, "name": c["name"]})
    # A company that was listed but whose board has emptied has usually moved to YC's own Work
    # at a Startup (Corgi, Remi). Only for those: polling YC pages for every company is too much.
    if norm(c["name"]) in PREVIOUS:
        try:
            jobs = FETCHERS["yc"](c["slug"], c["name"])
        except Exception:  # noqa: BLE001
            jobs = []
        if jobs:
            return {"ats": "yc", "slug": c["slug"], "count": len(jobs), "verified": True, "name": c["name"],
                    "batch": c.get("batch"), "industry": c.get("industry")}
    return first


def discover() -> list[dict]:
    http.RETRIES = 2
    yc = http.get_json(YC_HIRING)
    cands = [c for c in yc if c.get("status") == "Active" and c.get("isHiring")
             and US_REGIONS & set(c.get("regions") or [])]
    print(f"{len(cands)} YC companies hiring in the US or remotely", flush=True)
    with ThreadPoolExecutor(16) as pool:
        return [r for r in pool.map(find, cands) if r]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--found", help="JSON list of hits from an earlier run, instead of discovering")
    ap.add_argument("--out", default=os.path.join(ROOT, "companies_yc.toml"))
    args = ap.parse_args()

    if os.path.exists(args.out):
        with open(args.out, "rb") as f:
            PREVIOUS.update(norm(c["name"]) for c in tomllib.load(f).get("company", []))
    hits = json.load(open(args.found)) if args.found else discover()
    curated = []
    for name in ("companies.toml", "companies_gallery.toml"):  # a board listed twice stops the poller
        if os.path.exists(os.path.join(ROOT, name)):
            with open(os.path.join(ROOT, name), "rb") as f:
                curated += tomllib.load(f).get("company", [])
    taken ={(c["ats"], c["slug"].lower()) for c in curated} | {norm(c["name"]) for c in curated}

    keep, skipped = [], {"unverified": 0, "already listed": 0}
    for h in sorted(hits, key=lambda h: h["name"].lower()):
        if not h.get("verified"):
            skipped["unverified"] += 1
        elif (h["ats"], h["slug"].lower()) in taken or norm(h["name"]) in taken:
            skipped["already listed"] += 1
        else:
            taken |= {(h["ats"], h["slug"].lower()), norm(h["name"])}
            keep.append(h)

    lines = ["# Generated by scripts/import_yc.py from the YC directory (companies hiring in the US).",
             "# Every board was checked to belong to the company. Regenerate rather than hand-edit;",
             "# hand-picked companies go in companies.toml."]
    for h in keep:
        name = clean_name(h["name"]).replace('"', "'")
        lines += ["", "[[company]]", f'name = "{name}"', f'ats = "{h["ats"]}"', f'slug = "{h["slug"]}"']
        if h.get("batch"):
            lines[-4] += f'  # YC {h["batch"]}' + (f', {h["industry"]}' if h.get("industry") else "")
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    print(f"wrote {len(keep)} companies to {os.path.relpath(args.out, ROOT)}; skipped {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

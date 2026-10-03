"""Build companies_gallery.toml from the startups listed on startups.gallery.

    python scripts/import_gallery.py                 # read every company page (~5 min) and write companies_gallery.toml
    python scripts/import_gallery.py --found f.json  # reuse the boards found by an earlier run

The company list is the site's sitemap. Each company page links to the company's own job board
(jobs.ashbyhq.com/monaco, job-boards.greenhouse.io/k2spacecorporation, ...), so no slug is
guessed: the board linked most often on the page is taken, fetched, and kept if it has at least
one posting in the US or remote-US. Companies already in companies.toml or companies_yc.toml
are skipped.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import tomllib
import urllib.parse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from jobpoller import http  # noqa: E402
from jobpoller.ats import FETCHERS  # noqa: E402
from jobpoller.filters import Filters  # noqa: E402

SITEMAP = "https://startups.gallery/sitemap.xml"
OTHER_LISTS = ["companies.toml", "companies_yc.toml"]

# Board links as they appear on the pages, most specific first; group 1 is the slug.
S = r"([A-Za-z0-9][A-Za-z0-9_.%-]*)"
BOARDS = [
    ("ashby", rf"jobs\.ashbyhq\.com/{S}"),
    ("greenhouse", rf"(?:job-)?boards(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?{S}"),
    ("greenhouse", rf"boards-api\.greenhouse\.io/v1/boards/{S}"),
    ("lever", rf"jobs\.lever\.co/{S}"),
    ("lever", rf"jobs\.eu\.lever\.co/{S}"),  # slug gets the eu: prefix below
    ("workable", rf"apply\.workable\.com/{S}"),
    ("smartrecruiters", rf"(?:jobs|careers)\.smartrecruiters\.com/{S}"),
    ("rippling", rf"ats\.rippling\.com/(?:[a-z]{{2}}-[A-Z]{{2}}/)?{S}"),
    ("gem", rf"jobs\.gem\.com/{S}"),
    ("dover", rf"app\.dover\.com/(?:jobs|apply)/{S}"),
    ("recruitee", rf"//{S}\.recruitee\.com"),
    ("bamboohr", rf"//{S}\.bamboohr\.com"),
    ("breezy", rf"//{S}\.breezy\.hr"),
    ("pinpoint", rf"//{S}\.pinpointhq\.com"),
    ("teamtailor", rf"//{S}\.teamtailor\.com"),
    ("workday", r"//([a-z0-9-]+\.wd\d+\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?[A-Za-z0-9_-]+)"),
    ("yc", rf"ycombinator\.com/companies/{S}/jobs"),  # Work at a Startup: only when nothing else is linked
]
NOT_SLUGS = {"jobs", "job", "embed", "v1", "api", "www", "careers", "search", "apply", "j", "o", "p",
             "app", "static", "staticfe", "cdn", "assets"}  # the ATS's own subdomains
US = Filters(us_only=True, allow_unknown_location=False)


def norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def board_in(page: str) -> tuple[str, str] | None:
    """The board the page links to most, ignoring Work at a Startup if a real ATS is linked."""
    found = Counter()
    for ats, pat in BOARDS:
        for m in re.finditer(pat, page):
            slug = urllib.parse.unquote(m.group(1)).rstrip(".")  # Ashby boards can have spaces
            if slug.lower() in NOT_SLUGS:
                continue
            if ats == "lever" and "eu.lever" in m.group(0):
                slug = f"eu:{slug}"
            if ats == "workday":
                slug = re.sub(r"/[a-z]{2}-[A-Z]{2}/", "/", slug)
            found[(ats, slug)] += 1
    real = Counter({k: v for k, v in found.items() if k[0] != "yc"})
    return (real or found).most_common(1)[0][0] if found else None


# Links on a company page that aren't the company: the site itself, its CDN, socials, press.
NOISE = re.compile(r"framer|startups\.gallery|google|w3\.org|linkedin|tally\.so|gonzija|x\.com|twitter|"
                   r"youtube|instagram|facebook|crunchbase|techcrunch|businesswire|prnewswire|yahoo|finsmes|"
                   r"bloomberg|reuters|forbes|axios|wsj|nytimes|medium\.com|substack|github\.com", re.I)


def own_careers_pages(page: str) -> list[str]:
    """The company's own careers page, or its website + /careers, for pages that link no board."""
    links = [html.unescape(u) for u in re.findall(r'href="(https?://[^"]+)"', page) if not NOISE.search(u)]
    links = list(dict.fromkeys(links))
    careers = [u for u in links if re.search(r"career|jobs|join|hiring|open-roles", u, re.I)]
    if careers:
        return careers[:2]
    return [links[0].split("#")[0].rstrip("/") + "/careers"] if links else []


def read_page(slug: str) -> dict | None:
    try:
        page = http.get_text(f"https://startups.gallery/companies/{slug}", accept="text/html")
    except http.FetchError as e:
        return {"page": slug, "error": str(e)}
    title = re.search(r"<title>([^<|]+)", page)
    name = html.unescape(title.group(1)).strip() if title else slug
    board, via = board_in(page), None
    for url in [] if board else own_careers_pages(page):
        try:
            board, via = board_in(http.get_text(url, accept="text/html")), url
        except Exception:  # noqa: BLE001 - company sites fail in every way imaginable
            continue
        if board:
            break
    if not board:
        return {"page": slug, "name": name}
    return {"page": slug, "name": name, "ats": board[0], "slug": board[1], **({"via": via} if via else {})}


def check_board(h: dict) -> dict:
    """Fetch the board: how many postings, and how many in the US or remote-US."""
    if "ats" not in h:
        return h
    try:
        jobs = FETCHERS[h["ats"]](h["slug"], h["name"])
    except Exception as e:  # noqa: BLE001 - a dead link is common on a directory
        return {**h, "error": f"{type(e).__name__}: {e}"}
    return {**h, "count": len(jobs), "us": sum(US.location_ok(j) for j in jobs)}


def discover() -> list[dict]:
    http.RETRIES = 2
    pages = re.findall(r"<loc>https://startups\.gallery/companies/([^<]+)</loc>", http.get_text(SITEMAP, accept="text/xml"))
    print(f"{len(pages)} company pages", flush=True)
    with ThreadPoolExecutor(8) as pool:  # the pages are ~400 KB each; go easy on the site
        found = [h for h in pool.map(read_page, pages) if h]
    print(f"{sum('ats' in h for h in found)} link a board we can read; checking them", flush=True)
    with ThreadPoolExecutor(16) as pool:
        return list(pool.map(check_board, found))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--found", help="JSON from an earlier run's --save, instead of reading the site again")
    ap.add_argument("--save", help="also write every page's result to this JSON file")
    ap.add_argument("--out", default=os.path.join(ROOT, "companies_gallery.toml"))
    args = ap.parse_args()

    hits = json.load(open(args.found, encoding="utf-8")) if args.found else discover()
    if args.save:
        json.dump(hits, open(args.save, "w", encoding="utf-8"), indent=1)

    taken: set = set()
    for name in OTHER_LISTS:
        with open(os.path.join(ROOT, name), "rb") as f:
            for c in tomllib.load(f).get("company", []):
                taken |= {(c["ats"], c["slug"].lower()), norm(c["name"])}

    keep, skipped = [], Counter()
    for h in sorted(hits, key=lambda h: h.get("name", h["page"]).lower()):
        if "ats" not in h:
            skipped["no board linked" if "error" not in h else "page failed"] += 1
        elif "error" in h:
            skipped["board failed"] += 1
        elif not h.get("count"):
            skipped["board empty"] += 1
        elif not h.get("us"):
            skipped["nothing in the US"] += 1
        elif (h["ats"], h["slug"].lower()) in taken or norm(h["name"]) in taken:
            skipped["already listed"] += 1
        else:
            taken |= {(h["ats"], h["slug"].lower()), norm(h["name"])}
            keep.append(h)

    lines = ["# Generated by scripts/import_gallery.py from startups.gallery (each company's page links its board).",
             "# Only boards with a US or remote-US posting at import time. Regenerate rather than hand-edit;",
             "# hand-picked companies go in companies.toml."]
    for h in keep:
        name = h["name"].replace('"', "'")
        lines += ["", "[[company]]", f'name = "{name}"', f'ats = "{h["ats"]}"', f'slug = "{h["slug"]}"']
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    print(f"wrote {len(keep)} companies to {os.path.relpath(args.out, ROOT)}; skipped {dict(skipped)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

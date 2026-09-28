"""One fetcher per ATS. Each takes the company's board slug and returns every open posting as a Job.

All endpoints here are the public, unauthenticated ones the boards themselves use to render
careers pages. Nothing is scraped from HTML.
"""
from __future__ import annotations

import datetime as dt
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import quote

from .http import FetchError, get_json, get_text, post_json


@dataclass(frozen=True)
class Job:
    company: str
    id: str
    title: str
    url: str
    location: str = ""  # every location, joined with " | "
    remote: bool | None = None
    posted: str | None = None  # ISO date when the board gives one
    department: str | None = None


def _join(*parts) -> str:
    seen, out = set(), []
    for p in parts:
        p = (p or "").strip() if isinstance(p, str) else ""
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return " | ".join(out)


def _place(*parts) -> str:
    return ", ".join(p.strip() for p in parts if isinstance(p, str) and p.strip())


MAX_POSTINGS = 5000  # a board bigger than this is a marketplace or a wrong slug, not a company


def _check_size(n: int, slug: str) -> None:
    if n > MAX_POSTINGS:
        raise FetchError(f"{slug}: {n} postings is more than the {MAX_POSTINGS} limit; is this the right board?")


def _date(s) -> str | None:
    return s[:10] if isinstance(s, str) and re.match(r"\d{4}-\d{2}-\d{2}", s) else None


# --- the big three for startups -------------------------------------------------------------

def greenhouse(slug: str, company: str) -> list[Job]:
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    return [Job(company, str(j["id"]), j["title"].strip(), j["absolute_url"],
                location=(j.get("location") or {}).get("name", ""),
                posted=_date(j.get("first_published") or j.get("updated_at")))
            for j in data["jobs"]]


def lever(slug: str, company: str) -> list[Job]:
    host = "api.eu.lever.co" if slug.startswith("eu:") else "api.lever.co"
    slug = slug.removeprefix("eu:")
    jobs, skip = [], 0
    while True:  # Lever returns everything by default; paging keeps huge boards bounded
        page = get_json(f"https://{host}/v0/postings/{slug}?mode=json&limit=500&skip={skip}")
        for j in page:
            cat = j.get("categories") or {}
            jobs.append(Job(company, j["id"], j["text"].strip(), j["hostedUrl"],
                            location=_join(cat.get("location"), *(cat.get("allLocations") or [])),
                            remote=(j.get("workplaceType") == "remote") or None,
                            posted=_ms_date(j.get("createdAt")), department=cat.get("team")))
        if len(page) < 500:
            return jobs
        skip += 500
        _check_size(skip, slug)


def _ms_date(ms) -> str | None:
    if not isinstance(ms, (int, float)):
        return None
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).date().isoformat()


def ashby(slug: str, company: str) -> list[Job]:
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    out = []
    for j in data["jobs"]:
        if j.get("isListed") is False:
            continue
        addr = ((j.get("address") or {}).get("postalAddress") or {})
        out.append(Job(company, j["id"], j["title"].strip(), j["jobUrl"],
                       location=_join(j.get("location"),
                                      _place(addr.get("addressLocality"), addr.get("addressRegion"), addr.get("addressCountry")),
                                      *[s.get("location") for s in j.get("secondaryLocations") or []]),
                       remote=j.get("isRemote") or (j.get("workplaceType") == "Remote") or None,
                       posted=_date(j.get("publishedAt")), department=j.get("department")))
    return out


# --- other startup / mid-market boards -----------------------------------------------------

def workable(slug: str, company: str) -> list[Job]:
    data = get_json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}")
    out = []
    for j in data["jobs"]:
        locs = [_place(l.get("city"), l.get("region"), l.get("country")) for l in j.get("locations") or []]
        out.append(Job(company, j["shortcode"], j["title"].strip(), j["url"],
                       location=_join(_place(j.get("city"), j.get("state"), j.get("country")), *locs),
                       remote=bool(j.get("telecommuting")) or None,
                       posted=_date(j.get("published_on")), department=j.get("department")))
    return out


def smartrecruiters(slug: str, company: str) -> list[Job]:
    out, offset = [], 0
    while True:
        page = get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100&offset={offset}")
        _check_size(page.get("totalFound", 0), slug)
        for j in page["content"]:
            loc = j.get("location") or {}
            out.append(Job(company, j["id"], j["name"].strip(), f"https://jobs.smartrecruiters.com/{slug}/{j['id']}",
                           location=loc.get("fullLocation") or _place(loc.get("city"), loc.get("region"), loc.get("country")),
                           remote=loc.get("remote") or None, posted=_date(j.get("releasedDate")),
                           department=(j.get("department") or {}).get("label")))
        offset += len(page["content"])
        if not page["content"] or offset >= page.get("totalFound", 0):
            return out


def recruitee(slug: str, company: str) -> list[Job]:
    data = get_json(f"https://{slug}.recruitee.com/api/offers/")
    return [Job(company, str(j["id"]), j["title"].strip(), j["careers_url"],
                location=_join(j.get("location"), *[l.get("name") for l in j.get("locations") or []]),
                remote=j.get("remote") or None, posted=_date(j.get("published_at")),
                department=j.get("department"))
            for j in data["offers"] if j.get("status", "published") == "published"]


def bamboohr(slug: str, company: str) -> list[Job]:
    data = get_json(f"https://{slug}.bamboohr.com/careers/list", headers={"X-Requested-With": "XMLHttpRequest"})
    out = []
    for j in data["result"]:
        loc, ats = j.get("location") or {}, j.get("atsLocation") or {}
        out.append(Job(company, str(j["id"]), j["jobOpeningName"].strip(), f"https://{slug}.bamboohr.com/careers/{j['id']}",
                       location=_join(_place(loc.get("city"), loc.get("state")),
                                      _place(ats.get("city"), ats.get("state") or ats.get("province"), ats.get("country"))),
                       remote=bool(j.get("isRemote")) or j.get("locationType") == "1" or None,
                       department=j.get("departmentLabel")))
    return out


def breezy(slug: str, company: str) -> list[Job]:
    data = get_json(f"https://{slug}.breezy.hr/json")
    out = []
    for j in data:
        locs = [j.get("location") or {}, *(j.get("locations") or [])]
        out.append(Job(company, j["id"], j["name"].strip(), j["url"],
                       location=_join(*[l.get("name") for l in locs],
                                      *[(l.get("country") or {}).get("name") for l in locs]),
                       remote=any(l.get("is_remote") for l in locs) or None,
                       posted=_date(j.get("published_date")), department=j.get("department")))
    return out


def pinpoint(slug: str, company: str) -> list[Job]:
    data = get_json(f"https://{slug}.pinpointhq.com/postings.json")
    out = []
    for j in data["data"]:
        loc = j.get("location") or {}
        out.append(Job(company, str(j["id"]), j["title"].strip(), j["url"],
                       location=_join(loc.get("name"), _place(loc.get("city"), loc.get("province"))),
                       remote=(j.get("workplace_type") == "remote") or None,
                       department=((j.get("job") or {}).get("department") or {}).get("name")))
    return out


def rippling(slug: str, company: str) -> list[Job]:
    data = get_json(f"https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs")
    return [Job(company, j["uuid"], j["name"].strip(), j["url"],
                location=(j.get("workLocation") or {}).get("label", ""),
                department=(j.get("department") or {}).get("label"))
            for j in data]


def gem(slug: str, company: str) -> list[Job]:
    data = get_json(f"https://api.gem.com/job_board/v0/{slug}/job_posts/")
    return [Job(company, str(j["id"]), j["title"].strip(), j["absolute_url"],
                location=_join((j.get("location") or {}).get("name"),
                               *[(o.get("location") or {}).get("name") for o in j.get("offices") or []]),
                remote=(j.get("location_type") == "remote") or None,
                posted=_date(j.get("first_published_at")),
                department=((j.get("departments") or [{}])[0] or {}).get("name"))
            for j in data]


def dover(slug: str, company: str) -> list[Job]:
    page = get_json(f"https://app.dover.com/api/v1/careers-page-slug/{slug}")
    url = f"https://app.dover.com/api/v1/careers-page/{page['id']}/jobs"
    out = []
    while url:
        data = get_json(url)
        _check_size(data.get("count", 0), slug)
        for j in data["results"]:
            if not j.get("is_published", True) or j.get("is_sample"):
                continue
            locs = j.get("locations") or []
            out.append(Job(company, j["id"], j["title"].strip(), f"https://app.dover.com/apply/{slug}/{j['id']}",
                           location=_join(*[l.get("name") for l in locs]),
                           remote=(j.get("workplace_type") == "REMOTE") or None))
        url = data.get("next")
    return out


def manatal(slug: str, company: str) -> list[Job]:
    url = f"https://api.manatal.com/open/v3/career-page/{slug}/jobs/?page_size=100"
    out = []
    while url:
        data = get_json(url)
        _check_size(data.get("count", 0), slug)
        for j in data["results"]:
            out.append(Job(company, str(j["id"]), j["position_name"].strip(),
                           f"https://www.careers-page.com/{slug}/job/{j['hash']}",
                           location=j.get("location_display") or _place(j.get("city"), j.get("state"), j.get("country")),
                           remote=j.get("is_remote") or None))
        url = data.get("next")
    return out


TT = "{https://teamtailor.com/locations}"


def teamtailor(slug: str, company: str) -> list[Job]:
    host = slug if "." in slug else f"{slug}.teamtailor.com"
    root = ET.fromstring(get_text(f"https://{host}/jobs.rss", accept="application/rss+xml"))
    out = []
    for it in root.iter("item"):
        locs = [_place(l.findtext(f"{TT}city"), l.findtext(f"{TT}country")) or l.findtext(f"{TT}name")
                for l in it.iter(f"{TT}location")]
        out.append(Job(company, it.findtext("guid") or it.findtext("link"), (it.findtext("title") or "").strip(),
                       it.findtext("link") or "", location=_join(*locs),
                       remote=(it.findtext("remoteStatus") in ("fully", "remote")) or None,
                       posted=_rfc822_date(it.findtext("pubDate")), department=it.findtext(f"{TT}department")))
    return out


def _rfc822_date(s: str | None) -> str | None:
    try:
        return parsedate_to_datetime(s).date().isoformat() if s else None
    except (TypeError, ValueError):
        return None


# --- enterprise, for the few large companies worth watching --------------------------------

def workday(slug: str, company: str) -> list[Job]:
    """slug is the careers-site URL, e.g. nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite.

    Workday pages 20 postings per request and only reports the total on the first page.
    """
    host, site = _workday_parts(slug)
    tenant = host.split(".")[0]
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    out, offset, total = [], 0, None
    while total is None or offset < total:
        page = post_json(api, {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""})
        if total is None:
            total = page.get("total", 0)
            _check_size(total, slug)
        postings = page.get("jobPostings") or []
        for j in postings:
            if "externalPath" not in j:
                continue
            out.append(Job(company, j["externalPath"], j["title"].strip(), f"https://{host}/{site}{j['externalPath']}",
                           location=j.get("locationsText", "")))
        if not postings:
            break
        offset += len(postings)
    return out


def _workday_parts(slug: str) -> tuple[str, str]:
    s = re.sub(r"^https?://", "", slug).strip("/")
    host, _, path = s.partition("/")
    parts = [p for p in path.split("/") if p and not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", p)]
    if not host.endswith("myworkdayjobs.com") or not parts:
        raise FetchError(f"workday slug should look like tenant.wdN.myworkdayjobs.com/SiteName, got {slug!r}")
    return host, parts[0]


FETCHERS = {
    "greenhouse": greenhouse, "lever": lever, "ashby": ashby, "workable": workable,
    "smartrecruiters": smartrecruiters, "recruitee": recruitee, "bamboohr": bamboohr,
    "breezy": breezy, "pinpoint": pinpoint, "rippling": rippling, "gem": gem, "dover": dover,
    "manatal": manatal, "teamtailor": teamtailor, "workday": workday,
}

# Public board URL per ATS, for the discovery helper and for humans checking a slug.
BOARD_URL = {
    "greenhouse": "https://job-boards.greenhouse.io/{slug}",
    "lever": "https://jobs.lever.co/{slug}",
    "ashby": "https://jobs.ashbyhq.com/{slug}",
    "workable": "https://apply.workable.com/{slug}",
    "smartrecruiters": "https://jobs.smartrecruiters.com/{slug}",
    "recruitee": "https://{slug}.recruitee.com",
    "bamboohr": "https://{slug}.bamboohr.com/careers",
    "breezy": "https://{slug}.breezy.hr",
    "pinpoint": "https://{slug}.pinpointhq.com",
    "rippling": "https://ats.rippling.com/{slug}/jobs",
    "gem": "https://jobs.gem.com/{slug}",
    "dover": "https://app.dover.com/jobs/{slug}",
    "manatal": "https://www.careers-page.com/{slug}",
    "teamtailor": "https://{slug}.teamtailor.com/jobs",
    "workday": "https://{slug}",
}


def board_url(ats: str, slug: str) -> str:
    return BOARD_URL[ats].format(slug=quote(slug, safe="/.:"))

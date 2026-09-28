"""Each fetcher against a real (trimmed) response recorded from that ATS."""
import json
from pathlib import Path

import pytest

from jobpoller import ats

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


WRAP = {  # how each API wraps its list of postings
    "greenhouse": lambda x: {"jobs": x}, "ashby": lambda x: {"jobs": x}, "workable": lambda x: {"jobs": x},
    "smartrecruiters": lambda x: {"content": x, "totalFound": len(x)}, "recruitee": lambda x: {"offers": x},
    "bamboohr": lambda x: {"result": x}, "pinpoint": lambda x: {"data": x},
    "manatal": lambda x: {"results": x, "next": None}, "lever": lambda x: x, "breezy": lambda x: x,
    "rippling": lambda x: x, "gem": lambda x: x,
}


@pytest.mark.parametrize("name", sorted(WRAP))
def test_fetcher_parses_recorded_response(monkeypatch, name):
    calls = []

    def fake_get_json(url, **kw):
        calls.append(url)
        return WRAP[name](load(name))

    monkeypatch.setattr(ats, "get_json", fake_get_json)
    jobs = ats.FETCHERS[name]("acme", "Acme")
    assert len(jobs) == 2
    for j in jobs:
        assert j.company == "Acme"
        assert j.id and j.title and j.title == j.title.strip()
        assert j.url.startswith("https://")
        assert j.location, f"{name} lost the location"
    assert "acme" in calls[0]


def test_greenhouse_fields(monkeypatch):
    monkeypatch.setattr(ats, "get_json", lambda url, **kw: {"jobs": load("greenhouse")})
    j = ats.greenhouse("anthropic", "Anthropic")[0]
    assert j.id == "4461450008"
    assert j.url == "https://job-boards.greenhouse.io/anthropic/jobs/4461450008"
    assert j.posted == "2024-12-20"


def test_ashby_joins_primary_and_address(monkeypatch):
    monkeypatch.setattr(ats, "get_json", lambda url, **kw: {"jobs": load("ashby")})
    j = ats.ashby("openai", "OpenAI")[0]
    assert j.location == "San Francisco | San Francisco, California, United States"
    assert j.posted == "2026-03-12"


def test_lever_pages_until_short_page(monkeypatch):
    page = load("lever")
    urls = []

    def fake(url, **kw):
        urls.append(url)
        return page * 250 if "skip=0" in url else page  # 500 then 2

    monkeypatch.setattr(ats, "get_json", fake)
    assert len(ats.lever("palantir", "Palantir")) == 502
    assert [u.split("skip=")[1] for u in urls] == ["0", "500"]


def test_lever_eu_host(monkeypatch):
    urls = []
    monkeypatch.setattr(ats, "get_json", lambda url, **kw: urls.append(url) or [])
    ats.lever("eu:acme", "Acme")
    assert urls[0].startswith("https://api.eu.lever.co/v0/postings/acme?")


def test_smartrecruiters_pages_by_total(monkeypatch):
    item = load("smartrecruiters")[0]
    pages = {0: [item] * 100, 100: [item] * 30}
    monkeypatch.setattr(ats, "get_json", lambda url, **kw: {
        "content": pages[int(url.split("offset=")[1])], "totalFound": 130})
    jobs = ats.smartrecruiters("ServiceNow", "ServiceNow")
    assert len(jobs) == 130
    assert jobs[0].url == "https://jobs.smartrecruiters.com/ServiceNow/744000152242959"
    assert jobs[0].location == "Santa Clara, CALIFORNIA, United States"


def test_workday_pages_and_urls(monkeypatch):
    items = load("workday")
    bodies = []

    def fake(url, body, **kw):
        bodies.append(body)
        assert url == "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs"
        # the total only comes back on the first page, like the real API
        return {"total": 3 if body["offset"] == 0 else 0, "jobPostings": items if body["offset"] == 0 else items[:1]}

    monkeypatch.setattr(ats, "post_json", fake)
    jobs = ats.workday("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/", "NVIDIA")
    assert [b["offset"] for b in bodies] == [0, 2]
    assert len(jobs) == 3
    assert jobs[1].url == ("https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"
                           "/job/US-CA-Santa-Clara/Senior-DFX-Software-Engineer---Machine-Learning_JR2023503")
    assert jobs[1].location == "US, CA, Santa Clara"


def test_marketplace_sized_board_is_refused(monkeypatch):
    # Mercor posts ~16k contractor gigs on Manatal; fetching them 100 at a time would stall a run.
    monkeypatch.setattr(ats, "get_json", lambda url, **kw: {"count": 16614, "next": url, "results": load("manatal")})
    with pytest.raises(ats.FetchError, match="more than the 5000 limit"):
        ats.manatal("mercor", "Mercor")


def test_workday_rejects_bad_slug():
    with pytest.raises(ats.FetchError):
        ats._workday_parts("nvidia")


def test_dover_resolves_slug_then_pages(monkeypatch):
    items = load("dover")

    def fake(url, **kw):
        if "careers-page-slug" in url:
            return {"id": "abc"}
        if url.endswith("/abc/jobs"):
            return {"results": items[:1], "next": "https://app.dover.com/api/v1/careers-page/abc/jobs?page=2"}
        return {"results": items[1:] + [{**items[0], "id": "x", "is_sample": True}], "next": None}

    monkeypatch.setattr(ats, "get_json", fake)
    jobs = ats.dover("dover", "Dover")
    assert [j.title for j in jobs] == ["Head of People", "Marketing Leader"]  # sample posting dropped
    assert jobs[0].remote and jobs[0].location == "United States"


TEAMTAILOR_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:tt="https://teamtailor.com/locations"><channel>
<item><title>ML Engineer </title><pubDate>Thu, 23 Apr 2026 12:41:59 +0200</pubDate>
<link>https://acme.teamtailor.com/jobs/1-ml-engineer</link><remoteStatus>fully</remoteStatus>
<guid>fb63bcb7</guid><tt:locations><tt:location><tt:name>HQ</tt:name><tt:city>Austin</tt:city>
<tt:country>United States</tt:country></tt:location></tt:locations><tt:department>Eng</tt:department></item>
</channel></rss>"""


def test_teamtailor_rss(monkeypatch):
    urls = []
    monkeypatch.setattr(ats, "get_text", lambda url, **kw: urls.append(url) or TEAMTAILOR_RSS)
    [j] = ats.teamtailor("acme", "Acme")
    assert urls == ["https://acme.teamtailor.com/jobs.rss"]
    assert (j.id, j.title, j.location, j.remote, j.posted, j.department) == (
        "fb63bcb7", "ML Engineer", "Austin, United States", True, "2026-04-23", "Eng")

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
    assert j.posted == "2024-12-20T18:53:38Z"  # 13:53:38-05:00 in UTC


def test_ashby_joins_primary_and_address(monkeypatch):
    monkeypatch.setattr(ats, "get_json", lambda url, **kw: {"jobs": load("ashby")})
    j = ats.ashby("openai", "OpenAI")[0]
    assert j.location == "San Francisco | San Francisco, California, United States"
    assert j.posted == "2026-03-12T16:38:15Z"


@pytest.mark.parametrize("raw,expected", [
    ("2026-09-25 15:46:07 UTC", "2026-09-25T15:46:07Z"),      # Recruitee
    ("2026-09-24T17:17:22.405Z", "2026-09-24T17:17:22Z"),     # UKG, Breezy
    ("2026-09-28T17:49:51.367-07:00", "2026-09-29T00:49:51Z"),
    ("2026-07-30", "2026-07-30"),                             # Workable, Oracle: date only
    ("not a date", None), (None, None),
])
def test_posted_times_are_normalized_to_utc(raw, expected):
    assert ats._date(raw) == expected


@pytest.mark.parametrize("text,expected", [
    ("Posted Today", "2026-09-28"), ("Posted Yesterday", "2026-09-27"),
    ("Posted 3 Days Ago", "2026-09-25"), ("Posted 30+ Days Ago", "2026-08-29"), ("", None),
])
def test_workday_posted_text(text, expected):
    import datetime as dt
    assert ats._workday_posted(text, today=dt.date(2026, 9, 28)) == expected


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
        o = body["offset"]
        page = [{**items[i % 2], "externalPath": f"/job/{o + i}"} for i in range(min(20, 45 - o))]
        # the total only comes back on the first page, like the real API
        return {"total": 45 if o == 0 else 0, "jobPostings": page}

    monkeypatch.setattr(ats, "post_json", fake)
    jobs = ats.workday("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/", "NVIDIA")
    assert sorted(b["offset"] for b in bodies) == [0, 20, 40]
    assert [j.id for j in jobs] == [f"/job/{i}" for i in range(45)]  # in order despite parallel pages
    assert jobs[1].url == "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/1"
    assert jobs[1].location == "US, CA, Santa Clara"


EIGHTFOLD_V2 = {"id": 790298014263, "name": "AI Engineer 6 - Ads ", "location": "Remote, United States",
                "locations": ["Remote, United States"], "t_create": 1721692800, "department": "Ads",
                "canonicalPositionUrl": "https://explore.jobs.netflix.net/careers/job/790298014263"}
PCSX = {"id": 446720745160, "name": "SoC Physical Design Engineer", "locations": ["San Diego, California, United States"],
        "postedTs": 1790553600, "department": "Hardware Engineering", "workLocationOption": "onsite",
        "positionUrl": "/careers/job/446720745160"}


def test_eightfold_v2(monkeypatch):
    monkeypatch.setattr(ats, "EIGHTFOLD_DELAY", 0)
    urls = []

    def fake(url, **kw):
        urls.append(url)
        start = int(url.split("start=")[1].split("&")[0])
        return {"count": 25, "positions": [{**EIGHTFOLD_V2, "id": start + i} for i in range(min(10, 25 - start))]}

    monkeypatch.setattr(ats, "get_json", fake)
    jobs = ats.eightfold("explore.jobs.netflix.net/netflix.com", "Netflix")
    assert not isinstance(jobs, ats.Partial) and len(jobs) == 25  # small board: read whole
    assert urls[0] == "https://explore.jobs.netflix.net/api/apply/v2/jobs?domain=netflix.com&start=0&num=10&sort_by=new"
    j = jobs[0]
    assert (j.title, j.location, j.posted) == ("AI Engineer 6 - Ads", "Remote, United States", "2024-07-23T00:00:00Z")


def test_eightfold_falls_back_to_pcsx_and_reads_a_partial_window(monkeypatch):
    monkeypatch.setattr(ats, "EIGHTFOLD_DELAY", 0)
    urls = []

    def fake(url, **kw):
        urls.append(url)
        if "/api/apply/v2/" in url:
            raise ats.FetchError("HTTP 403", 403)
        start = int(url.split("start=")[1])
        return {"data": {"count": 2032, "positions": [{**PCSX, "id": start + i} for i in range(10)]}}

    monkeypatch.setattr(ats, "get_json", fake)
    jobs = ats.eightfold("careers.qualcomm.com/qualcomm.com", "Qualcomm")
    assert isinstance(jobs, ats.Partial) and len(jobs) == ats.EIGHTFOLD_PAGES * 10
    assert len(urls) == 1 + ats.EIGHTFOLD_PAGES  # the refused v2 call, then the window only
    assert jobs[0].url == "https://careers.qualcomm.com/careers/job/446720745160"  # host + positionUrl
    assert jobs[0].posted == "2026-09-28T00:00:00Z" and jobs[0].department == "Hardware Engineering"


def test_eightfold_requests_are_spaced_across_boards(monkeypatch):
    import threading, time
    monkeypatch.setattr(ats, "EIGHTFOLD_DELAY", 0.05)
    monkeypatch.setattr(ats, "_eightfold_next", 0.0)
    t = time.monotonic()
    threads = [threading.Thread(target=ats._eightfold_turn) for _ in range(4)]  # four boards at once
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert time.monotonic() - t >= 0.15  # 4 requests, 3 gaps, however many boards they came from


def test_eightfold_other_errors_are_not_swallowed(monkeypatch):
    def fake(url, **kw):
        raise ats.FetchError("HTTP 500", 500)
    monkeypatch.setattr(ats, "get_json", fake)
    with pytest.raises(ats.FetchError, match="500"):
        ats.eightfold("x.eightfold.ai/x.com", "X")


def test_oracle_pages_by_total(monkeypatch):
    req = {"Id": "R278941", "Title": "Software Engineer ", "PostedDate": "2026-09-28",
           "PrimaryLocation": "Round Rock, TX, United States", "WorkplaceType": "On-site",
           "secondaryLocations": [{"Name": "Austin, TX, United States"}]}
    urls = []

    def fake(url, **kw):
        urls.append(url)
        offset = int(url.split("offset=")[1].split(",")[0])
        n = min(200, 250 - offset)
        return {"items": [{"TotalJobsCount": 250, "requisitionList": [{**req, "Id": f"R{offset + i}"} for i in range(n)]}]}

    monkeypatch.setattr(ats, "get_json", fake)
    jobs = ats.oracle("enterpriseplatform.dell.com/CX_1001", "Dell")
    assert len(jobs) == 250 and len(urls) == 2
    assert "finder=findReqs;siteNumber=CX_1001,limit=200,offset=0" in urls[0]
    j = jobs[0]
    assert j.url == "https://enterpriseplatform.dell.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/R0"
    assert j.location == "Round Rock, TX, United States | Austin, TX, United States" and j.posted == "2026-09-28"


def test_ashby_slug_with_space_is_encoded(monkeypatch):
    urls = []
    monkeypatch.setattr(ats, "get_json", lambda url, **kw: urls.append(url) or {"jobs": []})
    ats.ashby("Hippocratic AI", "Hippocratic AI")
    assert urls == ["https://api.ashbyhq.com/posting-api/job-board/Hippocratic%20AI"]


def test_marketplace_sized_board_is_refused(monkeypatch):
    # Mercor posts ~16k contractor gigs on Manatal; fetching them 100 at a time would stall a run.
    monkeypatch.setattr(ats, "get_json", lambda url, **kw: {"count": 16614, "next": url, "results": load("manatal")})
    with pytest.raises(ats.FetchError, match="more than the 5000 limit"):
        ats.manatal("mercor", "Mercor")


def test_workday_search_terms_are_merged(monkeypatch):
    item = load("workday")[0]
    texts = []

    def fake(url, body, **kw):
        texts.append(body["searchText"])
        paths = {"software engineer": ["/job/a", "/job/b"], "data scientist": ["/job/b", "/job/c"]}[body["searchText"]]
        return {"total": 2, "jobPostings": [{**item, "externalPath": p} for p in paths]}

    monkeypatch.setattr(ats, "post_json", fake)
    jobs = ats.workday("walmart.wd504.myworkdayjobs.com/WalmartExternal", "Walmart",
                       search=("software engineer", "data scientist"))
    assert texts == ["software engineer", "data scientist"]
    assert [j.id for j in jobs] == ["/job/a", "/job/b", "/job/c"]


@pytest.mark.parametrize("text,path,expected", [
    ("2 Locations", "/job/India-Gurugram/SAP-Engineer_JR2026497", "India Gurugram (+1 more)"),  # emailed once
    ("3 Locations", "/job/US-CA-Santa-Clara/ML-Engineer_JR1", "US CA Santa Clara (+2 more)"),
    ("US, CA, Santa Clara", "/job/US-CA-Santa-Clara/ML-Engineer_JR1", "US, CA, Santa Clara"),
])
def test_workday_multi_location_uses_primary_from_path(text, path, expected):
    assert ats._workday_location({"locationsText": text, "externalPath": path}) == expected


def test_workday_skips_postings_without_a_title(monkeypatch):
    item = load("workday")[0]
    untitled = {k: v for k, v in item.items() if k != "title"}  # seen on Workday's own board
    monkeypatch.setattr(ats, "post_json", lambda url, body, **kw: {"total": 2, "jobPostings": [
        untitled, {**item, "externalPath": "/job/ok"}]})
    assert [j.id for j in ats.workday("workday.wd5.myworkdayjobs.com/Workday", "Workday")] == ["/job/ok"]


def test_ukg(monkeypatch):
    opp = {"Id": "4a96a59c", "Title": "Data Scientist ", "PostedDate": "2026-09-24T17:17:22.405Z", "JobCategoryName": "Data",
           "Locations": [{"LocalizedDescription": "General Remote - USA",
                          "Address": {"City": None, "State": None, "Country": {"Code": "USA", "Name": "United States"}}},
                         {"Address": {"City": "Phoenix", "State": {"Code": "AZ", "Name": "Arizona"},
                                      "Country": {"Code": "USA", "Name": "United States"}}}]}
    calls = []

    def fake(url, body, **kw):
        calls.append((url, body["opportunitySearch"]["Skip"]))
        return {"totalCount": 101, "opportunities": [{**opp, "Id": str(body["opportunitySearch"]["Skip"] + i)}
                                                     for i in range(min(100, 101 - body["opportunitySearch"]["Skip"]))]}

    monkeypatch.setattr(ats, "post_json", fake)
    board = "recruiting2.ultipro.com/WEB1004WEBIN/JobBoard/1786dc47"
    jobs = ats.ukg(board, "WebPT")
    assert [s for _, s in calls] == [0, 100] and len(jobs) == 101
    assert calls[0][0] == f"https://{board}/JobBoardView/LoadSearchResults"
    j = jobs[0]
    assert j.url == f"https://{board}/OpportunityDetail?opportunityId=0"
    assert j.location == "United States | Phoenix, Arizona, United States"
    assert (j.title, j.posted, j.department) == ("Data Scientist", "2026-09-24T17:17:22Z", "Data")


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
        "fb63bcb7", "ML Engineer", "Austin, United States", True, "2026-04-23T10:41:59Z", "Eng")


def test_jibe_pages_and_joins_locations(monkeypatch):
    data = load("jibe")
    urls = []

    def fake(url, **kw):
        urls.append(url)
        return {**data, "totalCount": 150}  # two pages of 100

    monkeypatch.setattr(ats, "get_json", fake)
    jobs = ats.jibe("jobs.keysight.com/external", "Keysight")
    assert sorted(u.split("page=")[1].split("&")[0] for u in urls) == ["1", "2"]
    assert urls[0].startswith("https://jobs.keysight.com/api/jobs?") and "sortBy=posted_date" in urls[0]
    multi, single = jobs[:2]
    assert multi.url == "https://jobs.keysight.com/external/jobs/53640"
    assert multi.location == "Santa Rosa, California, United States | Loveland, Colorado, United States"
    assert (multi.posted, multi.department) == ("2026-10-02T20:55:00Z", "R&D")
    assert single.location == "Atlanta, Georgia, United States"


def test_jibe_rejects_slug_without_path():
    with pytest.raises(ats.FetchError, match="host/path"):
        ats.jibe("careers.amd.com", "AMD")


def test_successfactors_rss(monkeypatch):
    urls = []
    monkeypatch.setattr(ats, "get_text", lambda url, **kw: urls.append(url) or (FIX / "successfactors.xml").read_text("utf-8"))
    jobs = ats.successfactors("careers.qorvo.com", "Qorvo")
    assert urls[0].startswith("https://careers.qorvo.com/services/rss/job/?locale=en_US&keywords=&rows=")
    j = jobs[1]
    assert (j.id, j.title, j.location, j.posted) == (
        "1397710500", "Principal Research Scientist -  RF MMIC Design", "Richardson, TX, US", "2026-10-02")
    assert j.url == "https://careers.qorvo.com/job/Richardson-Principal-Research-Scientist-RF-MMIC-Design-TX-75080/1397710500/"


def test_yc_reads_the_page_json(monkeypatch):
    urls = []
    monkeypatch.setattr(ats, "get_text", lambda url, **kw: urls.append(url) or (FIX / "yc.html").read_text("utf-8"))
    jobs = ats.yc("corgi-insurance", "Corgi Insurance")
    assert urls == ["https://www.ycombinator.com/companies/corgi-insurance/jobs"]
    j = jobs[1]
    assert (j.id, j.title, j.location, j.department) == (
        "113090", "Full Stack Engineer - New Product Line", "New York, NY, US", "Engineering")
    assert j.url == "https://www.ycombinator.com/companies/corgi-insurance/jobs/MrPadBh-full-stack-engineer-new-product-line"
    assert jobs[0].location.startswith("Phoenix, AZ, US | Philadelphia, PA, US | ")


def test_yc_page_without_data_is_an_error(monkeypatch):
    monkeypatch.setattr(ats, "get_text", lambda url, **kw: "<html>Just a moment...</html>")
    with pytest.raises(ats.FetchError, match="data-page"):
        ats.yc("corgi-insurance", "Corgi Insurance")


@pytest.mark.parametrize("text,expected", [
    ("less than a minute", "2026-10-02T18:00:00Z"), ("5 minutes", "2026-10-02T17:55:00Z"),
    ("about 23 hours", "2026-10-01T19:00:00Z"), ("1 day", "2026-10-01"), ("3 days", "2026-09-29"),
    ("about 1 month", "2026-09-02"), ("", None), (None, None),
])
def test_yc_posted_text(text, expected):
    import datetime as dt
    assert ats._yc_posted(text, now=dt.datetime(2026, 10, 2, 18, 0, tzinfo=dt.timezone.utc)) == expected

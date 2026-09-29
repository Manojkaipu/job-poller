import pytest

from jobpoller.ats import Job
from jobpoller.filters import Filters, is_us


def job(title="Engineer", location="", remote=None):
    return Job("Acme", "1", title, "https://x", location=location, remote=remote)


F = Filters(include=["machine learning", "ml engineer", "ml", "software engineer", "data scientist"],
            exclude=["senior", "sr", "staff", "principal", "manager", "director"])


@pytest.mark.parametrize("title,ok", [
    ("Machine Learning Engineer", True),
    ("Software Engineer, Search", True),
    ("ML Platform Engineer", True),
    ("Machine-Learning Engineer", True),     # hyphen counts as a space
    ("HTML Email Developer", False),         # "ml" only as a whole word
    ("Senior Software Engineer", False),
    ("Sr. Data Scientist", False),
    ("Engineering Manager, ML", False),
    ("Staff ML Engineer", False),
    ("Account Executive", False),
])
def test_titles(title, ok):
    assert F.title_ok(title) is ok


def test_protected_phrase_is_ignored_by_excludes_only():
    f = Filters(include=["member of technical staff", "robotics"], exclude=["staff", "senior"],
                protect=["member of technical staff"])
    assert f.title_ok("Member of Technical Staff, Inference")
    assert not f.title_ok("Senior Member of Technical Staff")
    assert not f.title_ok("Staff Robotics Software Engineer - Mission Autonomy")  # got through the old pairs


def test_no_include_means_everything():
    assert Filters(exclude=["senior"]).title_ok("Recruiter")


@pytest.mark.parametrize("place", [
    "US, CA, Santa Clara", "New York City, NY", "San Francisco", "Remote - US", "Austin, Texas, United States",
    "Santa Clara, CALIFORNIA, United States", "Scottsdale, Arizona", "Chaos, FL", "USA", "Seattle, WA (Hybrid)",
    "US-Remote", "Washington, DC",
])
def test_us_places(place):
    assert is_us(place)


@pytest.mark.parametrize("place", [
    "London, United Kingdom", "Toronto, ON", "Bengaluru, India", "Singapore, Singapore", "Remote - EMEA",
    "Paris, Île-de-France, France", "China, Shanghai", "Bangalore, IN", "Remote (Canada)",
])
def test_non_us_places(place):
    assert not is_us(place)


@pytest.mark.parametrize("location,remote,ok", [
    ("New York City, NY; San Francisco, CA", None, True),
    ("London; New York", None, True),                   # any US location is enough
    ("London, United Kingdom", None, False),
    ("Remote", None, True),                             # no country given: keep it
    ("Remote - EMEA", True, False),
    ("", None, True),                                   # unknown location: keep it by default
    ("Hybrid", None, True),                             # a work arrangement isn't a place (Cloudflare)
    ("In-Office", None, True),
    ("Hybrid | London", None, False),
    ("2 Locations", None, True),                        # Workday's summary for multi-city postings
    ("Paris, France", True, False),                     # remote, but in France
    ("Anywhere", None, True),
    # remote-flagged or "Remote" plus a place that isn't recognizably US: all seen in real matches
    ("Kosovo", True, False),
    ("Curitiba", True, False),
    ("Remote, Global", None, False),
    ("Remote - Ukraine | Ukraine, Ukraine", None, False),
    ("IN - Remote - IND", None, False),
    ("Remote ", None, True),
    ("Remote - US", None, True),
])
def test_location_filter(location, remote, ok):
    assert F.location_ok(job(location=location, remote=remote)) is ok


@pytest.mark.parametrize("location,shown", [
    ("London, UK; Ontario, CAN; Remote-Friendly, United States; San Francisco, CA",
     "Remote-Friendly, United States | San Francisco, CA (+2 outside the US)"),
    ("San Francisco, CA | New York City, NY", "San Francisco, CA | New York City, NY"),  # all US: unchanged
    ("Paris, France", "Paris, France"),                                                # no US: unchanged
])
def test_us_display(location, shown):
    from jobpoller.filters import us_display
    assert us_display(location) == shown


@pytest.mark.parametrize("location", ["India Gurugram (+1 more)", "Hybrid", ""])
def test_us_only_without_unknowns(location):
    f = Filters(allow_unknown_location=False)
    assert not f.location_ok(job(location=location))
    assert f.location_ok(job(location="US CA Santa Clara (+2 more)"))


def test_unknown_location_can_be_dropped():
    assert not Filters(allow_unknown_location=False).location_ok(job(location=""))


def test_extra_locations():
    f = Filters(extra_locations=["Toronto"])
    assert f.location_ok(job(location="Toronto, ON"))
    assert not f.location_ok(job(location="Vancouver, BC, Canada"))


def test_max_age():
    import datetime as dt
    now = dt.datetime(2026, 9, 28, 18, 0, tzinfo=dt.timezone.utc)
    f = Filters(max_age_hours=24)
    posted = lambda p: Job("A", "1", "x", "https://x", posted=p)
    assert f.fresh(posted("2026-09-28T09:00:00Z"), now)
    assert f.fresh(posted("2026-09-27T18:00:00Z"), now)       # exactly 24h
    assert not f.fresh(posted("2026-09-27T17:59:00Z"), now)
    assert f.fresh(posted("2026-09-27"), now)                 # date only: the cutoff's day counts
    assert not f.fresh(posted("2026-09-26"), now)
    assert f.fresh(posted(None), now)                         # no date: can't tell, keep
    assert Filters().fresh(posted("2020-01-01"), now)         # 0 = no limit


def test_us_only_off():
    assert Filters(us_only=False).location_ok(job(location="Berlin, Germany"))

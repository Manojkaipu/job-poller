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
    ("Paris, France", True, False),                     # remote, but in France
    ("Anywhere", None, True),
])
def test_location_filter(location, remote, ok):
    assert F.location_ok(job(location=location, remote=remote)) is ok


def test_unknown_location_can_be_dropped():
    assert not Filters(allow_unknown_location=False).location_ok(job(location=""))


def test_extra_locations():
    f = Filters(extra_locations=["Toronto"])
    assert f.location_ok(job(location="Toronto, ON"))
    assert not f.location_ok(job(location="Vancouver, BC, Canada"))


def test_us_only_off():
    assert Filters(us_only=False).location_ok(job(location="Berlin, Germany"))

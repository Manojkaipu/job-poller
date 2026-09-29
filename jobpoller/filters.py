"""Title keyword and location filters.

Keywords match whole words, case-insensitively, so "ml" matches "ML Engineer" but not "HTML".
The US check has to read many formats: "US, CA, Santa Clara", "New York City, NY",
"San Francisco", "Remote - US", "Remote (Canada)", "Austin, Texas, United States".
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from .ats import Job


def _terms(words: list[str]) -> re.Pattern | None:
    words = [w.strip() for w in words if w.strip()]
    if not words:
        return None
    alts = "|".join(re.escape(w).replace(r"\ ", r"[\s\-/]+") for w in sorted(words, key=len, reverse=True))
    return re.compile(rf"(?<![a-z0-9])(?:{alts})(?![a-z0-9])", re.I)


US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
}
US_CITIES = [
    "san francisco", "sf bay area", "bay area", "silicon valley", "new york city", "nyc", "brooklyn", "seattle",
    "bellevue", "redmond", "boston", "austin", "chicago", "los angeles", "palo alto", "mountain view",
    "menlo park", "sunnyvale", "san jose", "santa clara", "redwood city", "san mateo", "south san francisco",
    "oakland", "berkeley", "denver", "boulder", "phoenix", "scottsdale", "tempe", "chandler", "atlanta",
    "dallas", "houston", "miami", "pittsburgh", "philadelphia", "salt lake city", "san diego", "raleigh",
    "durham", "detroit", "minneapolis", "nashville", "columbus", "irvine", "santa monica", "washington dc",
    "washington, d.c.", "arlington",
]
# Anything here means "not the US" unless the text also says United States outright.
NON_US = [
    "canada", "toronto", "vancouver", "montreal", "ontario", "united kingdom", "uk", "england", "london",
    "scotland", "ireland", "dublin", "germany", "berlin", "munich", "france", "paris", "netherlands",
    "amsterdam", "spain", "madrid", "barcelona", "portugal", "lisbon", "italy", "milan", "switzerland",
    "zurich", "sweden", "stockholm", "denmark", "copenhagen", "norway", "finland", "poland", "warsaw",
    "romania", "czech", "prague", "austria", "vienna", "belgium", "israel", "tel aviv", "india", "bangalore",
    "bengaluru", "hyderabad", "pune", "mumbai", "delhi", "gurgaon", "gurugram", "noida", "chennai",
    "singapore", "japan", "tokyo", "korea", "seoul", "china", "shanghai", "beijing", "shenzhen", "hong kong",
    "taiwan", "taipei", "australia", "sydney", "melbourne", "new zealand", "brazil", "são paulo", "sao paulo",
    "mexico", "argentina", "buenos aires", "colombia", "bogota", "chile", "philippines", "manila", "vietnam",
    "indonesia", "thailand", "bangkok", "malaysia", "dubai", "uae", "saudi", "egypt", "nigeria", "kenya",
    "south africa", "emea", "europe", "eu", "apac", "latam", "latin america",
]

_NON_US = _terms(NON_US)
_US_WORDS = re.compile(r"united states|\busa\b|\bu\.s\.a?\.?(?![a-z])|\bamerica\b", re.I)
_US_TOKEN = re.compile(r"(?<![A-Za-z])US(?![A-Za-z])")  # case-sensitive: "US", "US-CA", "Remote US"
_STATE_NAMES = _terms(list(US_STATES))
_STATE_CODE = re.compile(r"(?:,|\s-|\()\s*(%s)(?![A-Za-z])" % "|".join(sorted(set(US_STATES.values()))))
_CITY = _terms(US_CITIES)
_SPLIT = re.compile(r"\s*(?:\||;|/|\bor\b)\s*")  # "London; New York", "SF/NYC", "Austin or Remote"
# Location text that names no place: work arrangements (Cloudflare) and Workday's "2 Locations".
_WORKPLACE_ONLY = re.compile(r"\b(?:hybrid|in[\s-]?office|on[\s-]?site|office|flexible|\d+\s+locations?)\b", re.I)
_REMOTE = re.compile(r"remote|anywhere|distributed|work from home|wfh", re.I)


def is_us(place: str) -> bool:
    if _US_WORDS.search(place) and not re.search(r"latin america", place, re.I):
        return True
    if _NON_US.search(place):
        return False
    return bool(_US_TOKEN.search(place) or _STATE_NAMES.search(place)
                or _STATE_CODE.search(place) or _CITY.search(place))


@dataclass
class Filters:
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    # Phrases blanked out before the exclude check, e.g. "member of technical staff" so that
    # "staff" can be excluded without dropping MTS roles.
    protect: list[str] = field(default_factory=list)
    us_only: bool = True
    extra_locations: list[str] = field(default_factory=list)  # also accepted, e.g. "Toronto"
    allow_unknown_location: bool = True
    # Drop postings the board says were posted longer ago than this (0 = no limit). Boards
    # that give no date can't be checked; for them, first seen by the poller is the proxy.
    max_age_hours: float = 0

    def __post_init__(self):
        self._inc, self._exc = _terms(self.include), _terms(self.exclude)
        self._protect, self._extra = _terms(self.protect), _terms(self.extra_locations)

    def title_ok(self, title: str) -> bool:
        if self._inc and not self._inc.search(title):
            return False
        rest = self._protect.sub(" ", title) if self._protect else title
        return not (self._exc and self._exc.search(rest))

    def location_ok(self, job: Job) -> bool:
        loc = job.location.strip()
        if not self.us_only:
            return True
        # Some boards put text in the location field that says nothing about the country.
        if not _WORKPLACE_ONLY.sub("", loc).strip(" |,;-()/"):
            return self.allow_unknown_location
        if self._extra and self._extra.search(loc):
            return True
        places = [p for p in _SPLIT.split(loc) if p.strip()]
        if any(is_us(p) for p in places):
            return True
        # "Remote" with no country attached counts; "Remote - EMEA" or "Remote (Canada)" doesn't.
        remote = job.remote or _REMOTE.search(loc)
        return bool(remote and not _NON_US.search(loc))

    def fresh(self, job: Job, now: dt.datetime) -> bool:
        if not self.max_age_hours or not job.posted:
            return True
        cutoff = now - dt.timedelta(hours=self.max_age_hours)
        if len(job.posted) == 10:  # date only: keep anything dated on or after the cutoff's day
            return job.posted >= cutoff.date().isoformat()
        return job.posted >= cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

    def match(self, job: Job) -> bool:
        return self.title_ok(job.title) and self.location_ok(job)

"""Find which ATS a company uses by trying likely board slugs on each one."""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

from .ats import FETCHERS, board_url

# Workday needs a tenant and site name that can't be guessed from the company name, so it's
# not tried here: copy it from the company's careers page URL instead.
ORDER = ["greenhouse", "ashby", "lever", "workable", "smartrecruiters", "recruitee", "bamboohr",
         "breezy", "pinpoint", "rippling", "gem", "dover", "manatal", "teamtailor"]


def slug_candidates(name: str, extra: list[str] = ()) -> list[str]:
    base = name.lower().replace("&", "and")
    words = re.findall(r"[a-z0-9]+", base)
    cands = [*extra, "".join(words), "-".join(words)]
    if len(words) > 1 and words[-1] in {"ai", "inc", "labs", "hq", "io", "technologies", "health"}:
        cands.append("".join(words[:-1]))
    return list(dict.fromkeys(c for c in cands if c))


def discover(name: str, extra_slugs: list[str] = (), ats: list[str] | None = None) -> list[dict]:
    tries = [(a, s) for a in (ats or ORDER) for s in slug_candidates(name, list(extra_slugs))]

    def attempt(t):
        a, s = t
        try:
            jobs = FETCHERS[a](s, name)
        except Exception:  # noqa: BLE001 - a miss is the common case here
            return None
        if not jobs:
            return None
        return {"ats": a, "slug": s, "count": len(jobs), "url": board_url(a, s),
                "sample": [f"{j.title} [{j.location}]" for j in jobs[:3]]}

    with ThreadPoolExecutor(max_workers=8) as pool:
        return [r for r in pool.map(attempt, tries) if r]

"""Load the config, fetch every board in parallel, diff against the state, and report what's new."""
from __future__ import annotations

import os
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .ats import FETCHERS, Job, Partial
from .filters import Filters
from .http import FetchError
from .state import BoardResult, State, now


@dataclass
class Company:
    name: str
    ats: str
    slug: str
    search: tuple[str, ...] = ()  # Workday only: search terms instead of the whole board

    @property
    def key(self) -> str:
        return f"{self.ats}:{self.slug}"


@dataclass
class Config:
    filters: Filters
    companies: list[Company]
    state_file: str = "state/seen.json"
    workers: int = 8
    alert_after_failures: int = 3


def load_config(path: str = "config.toml", companies_path: str | None = None) -> Config:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    base = os.path.dirname(os.path.abspath(path))
    companies_path = companies_path or os.path.join(base, cfg.get("settings", {}).get("companies_file", "companies.toml"))
    with open(companies_path, "rb") as f:
        raw = tomllib.load(f).get("company", [])

    companies, keys = [], set()
    for c in raw:
        if c["ats"] not in FETCHERS:
            raise ValueError(f"{c['name']}: unknown ats {c['ats']!r}; expected one of {', '.join(FETCHERS)}")
        co = Company(c["name"], c["ats"], c["slug"], tuple(c.get("search", ())))
        if co.search and co.ats != "workday":
            raise ValueError(f"{co.name}: `search` is only supported for workday boards")
        if co.key in keys:
            raise ValueError(f"{co.name}: board {co.key} is listed twice")
        keys.add(co.key)
        companies.append(co)

    s = cfg.get("settings", {})
    f = cfg.get("filters", {})
    state_file = s.get("state_file", "state/seen.json")
    return Config(
        filters=Filters(include=f.get("include", []), exclude=f.get("exclude", []), protect=f.get("protect", []),
                        us_only=f.get("us_only", True), extra_locations=f.get("extra_locations", []),
                        allow_unknown_location=f.get("allow_unknown_location", True)),
        companies=companies,
        state_file=os.path.normpath(os.path.join(base, state_file)),
        workers=s.get("workers", 8),
        alert_after_failures=s.get("alert_after_failures", 3),
    )


@dataclass
class Fetched:
    company: Company
    jobs: list[Job] = field(default_factory=list)
    error: str | None = None
    seconds: float = 0.0
    partial: bool = False  # only the newest postings were read (see ats.Partial)


def fetch_one(c: Company) -> Fetched:
    t = time.monotonic()
    try:
        jobs = FETCHERS[c.ats](c.slug, c.name, **({"search": c.search} if c.search else {}))
        # Some boards list one posting under several locations with the same id; keep the first.
        unique: dict[str, Job] = {}
        for j in jobs:
            unique.setdefault(j.id, j)
        return Fetched(c, list(unique.values()), seconds=time.monotonic() - t, partial=isinstance(jobs, Partial))
    except FetchError as e:
        return Fetched(c, error=str(e), seconds=time.monotonic() - t)
    except Exception as e:  # a board changing its format shouldn't take the whole run down
        return Fetched(c, error=f"{type(e).__name__}: {e}", seconds=time.monotonic() - t)


def fetch_all(companies: list[Company], workers: int = 8) -> list[Fetched]:
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(fetch_one, companies))


@dataclass
class RunReport:
    new: list[Job]
    problems: list[str]
    baselined: dict[str, int]
    fetched: list[Fetched]
    alert_problems: bool  # True when a board has just crossed the failure threshold


def run(cfg: Config, state: State, log=print) -> RunReport:
    fetched = fetch_all(cfg.companies, cfg.workers)
    when = now()
    new, baselined, problems, alert_problems = [], {}, [], False

    for f in fetched:
        key = f.company.key
        if f.error:
            state.record(BoardResult(key, ok=False, error=f.error), when)
            fails = state.boards[key]["failures"]
            log(f"  ! {f.company.name:28} {f.error}  (failure {fails})")
            if fails >= cfg.alert_after_failures:
                problems.append(f"{f.company.name} ({key}): failing for {fails} runs: {f.error}")
                alert_problems |= fails == cfg.alert_after_failures
            continue

        ids = [j.id for j in f.jobs]
        if state.is_baseline(key):
            baselined[f.company.name] = len(ids)
        else:
            fresh = state.new_ids(key, ids)
            if f.partial:
                # An old posting can slide into a partial window when newer ones close; only
                # count it if it was posted after this board was first polled.
                since = state.boards[key]["first_run"][:10]
                fresh = {j.id for j in f.jobs if j.id in fresh and j.posted and j.posted >= since}
            new += [j for j in f.jobs if j.id in fresh and cfg.filters.match(j)]
            prev = state.boards[key].get("count") or 0
            if not ids and prev >= 5:
                problems.append(f"{f.company.name} ({key}): 0 postings, had {prev}. Did the board move?")
        state.record(BoardResult(key, ok=True, job_ids=ids), when)

    state.prune(when, keep_boards={c.key for c in cfg.companies})
    return RunReport(new, problems, baselined, fetched, alert_problems)


def summarize(report: RunReport, log=print) -> None:
    ok = [f for f in report.fetched if not f.error]
    total = sum(len(f.jobs) for f in ok)
    slow = max(report.fetched, key=lambda f: f.seconds, default=None)
    log(f"{len(ok)}/{len(report.fetched)} boards fetched, {total} postings"
        + (f"; slowest {slow.company.name} {slow.seconds:.1f}s" if slow else ""))
    if report.baselined:
        log(f"first run for {len(report.baselined)} board(s), recorded without alerting: "
            + ", ".join(f"{k} ({v})" for k, v in sorted(report.baselined.items())))
    log(f"{len(report.new)} new matching posting(s)")


def print_jobs(jobs: list[Job], out=sys.stdout) -> None:
    for j in sorted(jobs, key=lambda j: (j.company.lower(), j.title.lower())):
        extra = " · ".join(x for x in (j.location, "remote" if j.remote else "", j.posted or "") if x)
        print(f"{j.company:22} {j.title}\n{'':22} {extra}\n{'':22} {j.url}", file=out)

"""Load the config, fetch every board in parallel, diff against the state, and report what's new."""
from __future__ import annotations

import dataclasses
import datetime as dt
import os
import re
import sys
import time
import tomllib
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .ats import FETCHERS, Job, Partial
from .filters import Filters, us_display
from .http import GONE_STATUS, FetchError
from .notify import posted_label
from .state import BoardResult, State, now


@dataclass
class Company:
    name: str
    ats: str
    slug: str
    search: tuple[str, ...] = ()  # Workday only: search terms instead of the whole board
    facets: dict[str, list[str]] = field(default_factory=dict)  # Workday only: e.g. job categories

    @property
    def key(self) -> str:
        return f"{self.ats}:{self.slug}"


@dataclass
class Profile:
    """A recipient who wants more than the base filters: everything they match, plus postings
    whose titles hit the profile's own terms (with the base excludes and its own extra ones)."""
    name: str
    to_env: str  # environment variable (a repo secret in CI) holding the address(es)
    base: Filters
    extra: Filters

    def match(self, job: Job) -> bool:
        return self.base.match(job) or self.extra.match(job)

    def fresh(self, job: Job, now: dt.datetime) -> bool:
        return self.base.fresh(job, now)

    @property
    def max_age_hours(self) -> float:
        return self.base.max_age_hours


@dataclass
class Config:
    filters: Filters
    companies: list[Company]
    profiles: list[Profile] = field(default_factory=list)
    state_file: str = "state/seen.json"
    workers: int = 8
    alert_after_failures: int = 3  # for a board that's gone (404, 410, 422)
    alert_after_outage: int = 24  # for anything else: maintenance pages, 5xx, 429, timeouts


def load_config(path: str = "config.toml", companies_path: str | None = None) -> Config:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    base = os.path.dirname(os.path.abspath(path))
    files = companies_path or cfg.get("settings", {}).get("companies_file", "companies.toml")
    raw = []
    for name in [files] if isinstance(files, str) else files:  # one file, or a list of them
        with open(os.path.join(base, name), "rb") as f:
            raw += tomllib.load(f).get("company", [])

    companies, keys = [], set()
    for c in raw:
        if c["ats"] not in FETCHERS:
            raise ValueError(f"{c['name']}: unknown ats {c['ats']!r}; expected one of {', '.join(FETCHERS)}")
        co = Company(c["name"], c["ats"], c["slug"], tuple(c.get("search", ())), dict(c.get("facets", {})))
        if (co.search or co.facets) and co.ats != "workday":
            raise ValueError(f"{co.name}: `search` and `facets` are only supported for workday boards")
        if co.key in keys:
            raise ValueError(f"{co.name}: board {co.key} is listed twice")
        keys.add(co.key)
        companies.append(co)

    s = cfg.get("settings", {})
    f = cfg.get("filters", {})
    state_file = s.get("state_file", "state/seen.json")
    filters = Filters(include=f.get("include", []), exclude=f.get("exclude", []), protect=f.get("protect", []),
                   us_only=f.get("us_only", True), extra_locations=f.get("extra_locations", []),
                   allow_unknown_location=f.get("allow_unknown_location", True),
                   max_age_hours=f.get("max_age_hours", 0))
    profiles = []
    for p in cfg.get("profile", []):
        if not p.get("include_extra") or not p.get("to_env"):
            raise ValueError(f"profile {p.get('name')!r} needs `to_env` and `include_extra`")
        profiles.append(Profile(p["name"], p["to_env"], filters, dataclasses.replace(
            filters, include=p["include_extra"], exclude=[*filters.exclude, *p.get("exclude_extra", [])])))
    return Config(
        filters=filters,
        companies=companies,
        profiles=profiles,
        state_file=os.path.normpath(os.path.join(base, state_file)),
        workers=s.get("workers", 8),
        alert_after_failures=s.get("alert_after_failures", 3),
        alert_after_outage=s.get("alert_after_outage", 24),
    )


@dataclass
class Fetched:
    company: Company
    jobs: list[Job] = field(default_factory=list)
    error: str | None = None
    seconds: float = 0.0
    partial: bool = False  # only the newest postings were read (see ats.Partial)
    gone: bool = False  # the error says the board itself is gone, not that the ATS is having a bad hour


def fetch_one(c: Company) -> Fetched:
    t = time.monotonic()
    try:
        kw = {k: v for k, v in (("search", c.search), ("facets", c.facets)) if v}
        jobs = FETCHERS[c.ats](c.slug, c.name, **kw)
        # Some boards list one posting under several locations with the same id; keep the first.
        unique: dict[str, Job] = {}
        for j in jobs:
            unique.setdefault(j.id, j)
        return Fetched(c, list(unique.values()), seconds=time.monotonic() - t, partial=isinstance(jobs, Partial))
    except FetchError as e:
        return Fetched(c, error=str(e), seconds=time.monotonic() - t, gone=e.status in GONE_STATUS)
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
    profile_new: dict[str, list[Job]] = field(default_factory=dict)  # profile name -> its new postings


def run(cfg: Config, state: State, log=print) -> RunReport:
    run_started = dt.datetime.now(dt.timezone.utc)
    fetched = fetch_all(cfg.companies, cfg.workers)
    when = now()
    new, baselined, problems, alert_problems = [], {}, [], False
    unseen_jobs: list[Job] = []  # new to the poller, before any filter: what profiles pick from
    failing: dict[tuple[str, str], list[tuple[Fetched, int]]] = defaultdict(list)

    for f in fetched:
        key = f.company.key
        if f.error:
            state.record(BoardResult(key, ok=False, error=f.error), when)
            fails = state.boards[key]["failures"]
            log(f"  ! {f.company.name:28} {f.error}  (failure {fails})")
            threshold = cfg.alert_after_failures if f.gone else cfg.alert_after_outage
            if fails >= threshold:
                failing[(f.company.ats, _error_kind(f.error))].append((f, fails))
                alert_problems |= fails == threshold
            continue

        ids = [j.id for j in f.jobs]
        if state.is_baseline(key):
            baselined[f.company.name] = len(ids)
        else:
            unseen = state.new_ids(key, ids)
            if f.partial:
                # An old posting can slide into a partial window when newer ones close; only
                # count it if it was posted after this board was first polled.
                since = state.boards[key]["first_run"][:10]
                unseen = {j.id for j in f.jobs if j.id in unseen and j.posted and j.posted >= since}
            unseen_jobs += [j for j in f.jobs if j.id in unseen]
            new += [j for j in f.jobs
                    if j.id in unseen and cfg.filters.match(j) and cfg.filters.fresh(j, run_started)]
            prev = state.boards[key].get("count") or 0
            if not ids and prev >= 5:
                problems.append(f"{f.company.name} ({key}): 0 postings, had {prev}. Did the board move?")
        state.record(BoardResult(key, ok=True, job_ids=ids), when)

    problems = _failure_lines(failing) + problems
    state.prune(when, keep_boards={c.key for c in cfg.companies})
    profile_new = {p.name: [j for j in unseen_jobs if p.match(j) and p.fresh(j, run_started)]
                   for p in cfg.profiles}
    return RunReport(new, problems, baselined, fetched, alert_problems, profile_new)


GROUP_AT = 3  # this many boards on one ATS failing the same way is the ATS, not the boards


def _error_kind(error: str) -> str:
    """The error without its URL, so the same failure on different boards compares equal."""
    return re.sub(r"\s*(from )?https?://\S+", "", error).strip()


def _failure_lines(failing: dict[tuple[str, str], list[tuple[Fetched, int]]]) -> list[str]:
    lines = []
    for (ats, kind), items in failing.items():
        if len(items) >= GROUP_AT:
            names = ", ".join(f.company.name for f, _ in items)
            runs = min(n for _, n in items)
            lines.append(f"{ats}: {len(items)} boards failing for {runs}+ runs, probably an outage on {ats}'s side "
                         f"rather than the boards: {kind} ({names})")
        else:
            lines += [f"{f.company.name} ({f.company.key}): failing for {n} runs: {f.error}" for f, n in items]
    return lines


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
    for name, jobs in report.profile_new.items():
        log(f"{len(jobs)} new for the {name} profile")


def print_jobs(jobs: list[Job], out=sys.stdout) -> None:
    for j in sorted(jobs, key=lambda j: (j.company.lower(), j.title.lower())):
        extra = " · ".join(x for x in (us_display(j.location), "remote" if j.remote else "", posted_label(j)) if x)
        print(f"{j.company:22} {j.title}\n{'':22} {extra}\n{'':22} {j.url}", file=out)

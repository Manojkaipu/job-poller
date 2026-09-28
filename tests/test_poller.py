"""The run loop end to end with fake boards: baseline, diffing, failures, pruning, config."""
import textwrap

import pytest

from jobpoller import poller
from jobpoller.ats import Job, Partial
from jobpoller.filters import Filters
from jobpoller.http import FetchError
from jobpoller.poller import Company, Config, load_config, run
from jobpoller.state import State, now


def j(i, title="ML Engineer", location="San Francisco, CA"):
    return Job("Acme", str(i), title, f"https://x/{i}", location=location)


class Boards:
    """Stand-in for FETCHERS: what each board returns on the next run."""

    def __init__(self):
        self.jobs = {}

    def fetch(self, slug, company):
        r = self.jobs[slug]
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def setup(tmp_path, monkeypatch):
    boards = Boards()
    monkeypatch.setitem(poller.FETCHERS, "fake", boards.fetch)
    cfg = Config(filters=Filters(include=["ml engineer"], exclude=["senior"]),
                 companies=[Company("Acme", "fake", "acme")], state_file=str(tmp_path / "seen.json"),
                 alert_after_failures=2)
    return boards, cfg


def cycle(cfg):
    state = State(cfg.state_file)
    report = run(cfg, state, log=lambda *a: None)
    state.save()
    return report


def test_first_run_is_a_silent_baseline_then_only_new_postings_alert(setup):
    boards, cfg = setup
    boards.jobs["acme"] = [j(1), j(2)]
    r = cycle(cfg)
    assert r.new == [] and r.baselined == {"Acme": 2}

    boards.jobs["acme"] = [j(1), j(2), j(3), j(4, "Senior ML Engineer"), j(5, "Recruiter"), j(6, location="Berlin, Germany")]
    r = cycle(cfg)
    assert [x.id for x in r.new] == ["3"]  # 4-6 are new but filtered out

    r = cycle(cfg)
    assert r.new == []  # nothing reported twice


def test_filtered_out_postings_stay_known_when_filters_change(setup):
    boards, cfg = setup
    boards.jobs["acme"] = [j(1)]
    cycle(cfg)
    boards.jobs["acme"] = [j(1), j(2, "Recruiter")]
    cycle(cfg)
    cfg.filters = Filters()  # widen the filters: the recruiter post isn't new any more
    assert cycle(cfg).new == []


def test_failed_fetch_keeps_postings_and_flags_after_threshold(setup):
    boards, cfg = setup
    boards.jobs["acme"] = [j(1), j(2)]
    cycle(cfg)
    boards.jobs["acme"] = FetchError("HTTP 503")
    r1 = cycle(cfg)
    assert r1.problems == [] and not r1.alert_problems
    r2 = cycle(cfg)
    assert len(r2.problems) == 1 and r2.alert_problems  # threshold crossed: email once
    r3 = cycle(cfg)
    assert len(r3.problems) == 1 and not r3.alert_problems  # still listed, but no email on its own

    boards.jobs["acme"] = [j(1), j(2), j(3)]
    r = cycle(cfg)
    assert [x.id for x in r.new] == ["3"]  # 1 and 2 weren't forgotten during the outage


def test_a_board_that_failed_on_its_first_run_still_baselines(setup):
    boards, cfg = setup
    boards.jobs["acme"] = FetchError("HTTP 404")
    cycle(cfg)
    boards.jobs["acme"] = [j(1)]
    assert cycle(cfg).baselined == {"Acme": 1}


def test_empty_board_is_flagged(setup):
    boards, cfg = setup
    boards.jobs["acme"] = [j(i) for i in range(6)]
    cycle(cfg)
    boards.jobs["acme"] = []
    assert "0 postings, had 6" in cycle(cfg).problems[0]


def test_unexpected_parser_error_is_contained(setup):
    boards, cfg = setup
    boards.jobs["acme"] = KeyError("jobs")
    r = cycle(cfg)
    assert r.fetched[0].error == "KeyError: 'jobs'"


def test_duplicate_ids_on_a_board_are_collapsed(setup):
    boards, cfg = setup
    boards.jobs["acme"] = [j(1), j(1, location="New York, NY")]
    assert len(cycle(cfg).fetched[0].jobs) == 1


def test_partial_board_only_reports_postings_newer_than_its_first_run(setup):
    boards, cfg = setup
    today = now()[:10]
    old = Job("Acme", "old", "ML Engineer", "https://x/old", location="Austin, TX", posted="2020-01-01")
    fresh = Job("Acme", "new", "ML Engineer", "https://x/new", location="Austin, TX", posted=today)
    undated = Job("Acme", "undated", "ML Engineer", "https://x/u", location="Austin, TX")
    boards.jobs["acme"] = Partial([j(1)])
    cycle(cfg)
    # an older posting slides into the window as newer ones close; it must not be "new"
    boards.jobs["acme"] = Partial([fresh, old, undated])
    assert [x.id for x in cycle(cfg).new] == ["new"]


def test_prune_forgets_old_postings_and_removed_boards(tmp_path):
    s = State(str(tmp_path / "s.json"))
    s.jobs = {"a:x": {"old": ["2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00"],
                      "live": ["2026-01-01T00:00:00+00:00", "2026-03-01T00:00:00+00:00"]},
              "b:gone": {"1": ["2026-03-01T00:00:00+00:00", "2026-03-01T00:00:00+00:00"]}}
    s.boards = {"a:x": {}, "b:gone": {}}
    assert s.prune("2026-03-01T00:00:00+00:00", keep_boards={"a:x"}) == 2
    assert list(s.jobs) == ["a:x"] and list(s.jobs["a:x"]) == ["live"] and "b:gone" not in s.boards


def test_load_config(tmp_path):
    (tmp_path / "config.toml").write_text(textwrap.dedent("""
        [settings]
        state_file = "state/seen.json"
        [filters]
        include = ["ml engineer"]
        us_only = false
    """))
    (tmp_path / "companies.toml").write_text(textwrap.dedent("""
        [[company]]
        name = "Acme"
        ats = "greenhouse"
        slug = "acme"
    """))
    cfg = load_config(str(tmp_path / "config.toml"))
    assert cfg.companies == [Company("Acme", "greenhouse", "acme")]
    assert cfg.state_file == str(tmp_path / "state" / "seen.json")
    assert cfg.filters.us_only is False


def test_config_rejects_unknown_ats_and_duplicates(tmp_path):
    (tmp_path / "config.toml").write_text("")
    (tmp_path / "companies.toml").write_text('[[company]]\nname="A"\nats="taleo"\nslug="a"\n')
    with pytest.raises(ValueError, match="unknown ats"):
        load_config(str(tmp_path / "config.toml"))
    (tmp_path / "companies.toml").write_text('[[company]]\nname="A"\nats="lever"\nslug="a"\n' * 2)
    with pytest.raises(ValueError, match="listed twice"):
        load_config(str(tmp_path / "config.toml"))
    (tmp_path / "companies.toml").write_text('[[company]]\nname="A"\nats="lever"\nslug="a"\nsearch=["x"]\n')
    with pytest.raises(ValueError, match="only supported for workday"):
        load_config(str(tmp_path / "config.toml"))


def test_search_terms_reach_the_workday_fetcher(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setitem(poller.FETCHERS, "workday", lambda slug, company, search=(): seen.update(search=search) or [])
    poller.fetch_one(Company("W", "workday", "w.wd5.myworkdayjobs.com/X", ("software engineer",)))
    assert seen["search"] == ("software engineer",)
